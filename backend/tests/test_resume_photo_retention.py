"""A1: after `/agent/resume`, the confirmed photo is moved out of staging (Issue #176).

`/agent/resume` replies first; then, in the background, C1's post-commit trigger
(PR #201) runs its on-demand worker over B1's work items, calling A2's idempotent
`retain_media`. Only a written result starts it; a failure leaves the work item
pending and never changes the reply. Cosmos is faked; the approval service, A1's
cards, C1's builder, trigger and worker, and B1's storage are the real ones.
"""

from datetime import datetime

from fastapi.testclient import TestClient
from test_card_photos import (
    PHOTO,
    FakeMediaCosmosContainer,
    _agent,
    _ask,
    _bought,
    _mapping_document,
    setup,
)
from test_purchase_cards import HOME, resume_app

from app.core import dependencies
from app.core.dependencies import (
    _create_media_retention_trigger,
    _reset_media_retention_trigger_for_test,
    get_media_retention_trigger,
)
from app.services.cosmos_write import CosmosWriteRepository
from app.services.shopping.media_handoff import MediaRetentionPostCommitTrigger


class Container(FakeMediaCosmosContainer):
    """Also lists due work items the way B1's query does: pending and due only."""

    async def query_items(self, *, query, parameters, partition_key):
        values = {p["name"]: p["value"] for p in parameters}
        if values.get("@record_type") == "media_retention_work_item":
            now = datetime.fromisoformat(str(values["@now"]).replace("Z", "+00:00"))
            return [
                dict(d)
                for (h, _), d in self.documents.items()
                if h == partition_key == values["@household_id"]
                and d.get("record_type") == "media_retention_work_item"
                and d.get("status") == values["@status"]
                and datetime.fromisoformat(str(d["next_attempt_at"]).replace("Z", "+00:00")) <= now
            ][: int(values.get("@limit", 100))]
        return await super().query_items(
            query=query, parameters=parameters, partition_key=partition_key
        )


class Media:
    """A2's `retain_media`: records each call; can fail like a Blob outage."""

    def __init__(self, error: Exception | None = None) -> None:
        self.retained: list[tuple[str, str]] = []
        self.error = error

    async def retain_media(self, household_id: str, image_ref: str) -> bool:
        self.retained.append((household_id, image_ref))
        if self.error is not None:
            raise self.error
        return True


def _photo_card(monkeypatch, *records):
    """A one-line purchase card that keeps the photo, and what confirming needs."""
    monkeypatch.setattr("test_card_photos.FakeMediaCosmosContainer", Container)
    container, reader, approvals, storage = setup(*records)
    model = _bought({"shopping_item_id": "tissue-1", "quantity": 2})
    card = _ask(_agent(model, reader, approvals, storage), "我買了衛生紙 2 包").pending_approval
    assert card.operations[0].values["image_ref"] == PHOTO
    return container, approvals, card


def _app(container, approvals, media):
    trigger = _create_media_retention_trigger(
        CosmosWriteRepository(container, query_client=container), container, media
    )
    app = resume_app(approvals)
    app.dependency_overrides[get_media_retention_trigger] = lambda: trigger
    return app


def _work_item(container):
    [item] = container.of_type("media_retention_work_item")
    return item


def _resume(client, card, approved=True):
    body = {"household_id": HOME, "approval_id": card.approval_id, "approved": approved}
    return client.post("/agent/resume", json=body)


def test_confirming_moves_the_photo_after_replying(monkeypatch):
    container, approvals, card = _photo_card(monkeypatch)
    media = Media()

    with TestClient(_app(container, approvals, media)) as client:
        response = _resume(client, card)

    assert response.status_code == 200
    assert response.json()["message"] == "已記錄：衛生紙 +2 包（現在 3 包）"
    assert media.retained == [(HOME, PHOTO)]
    assert _work_item(container)["status"] == "completed"
    assert container.get("inventory_item:tissue")["image_ref"] == PHOTO


def test_a_second_confirm_moves_nothing_again(monkeypatch):
    container, approvals, card = _photo_card(monkeypatch)
    media = Media()

    with TestClient(_app(container, approvals, media)) as client:
        first = _resume(client, card)
        again = _resume(client, card)  # a double tap or a retry

    assert again.json()["message"] == first.json()["message"]
    assert media.retained == [(HOME, PHOTO)]
    assert _work_item(container)["status"] == "completed"


def test_cancelling_moves_nothing(monkeypatch):
    container, approvals, card = _photo_card(monkeypatch)
    media = Media()

    with TestClient(_app(container, approvals, media)) as client:
        response = _resume(client, card, approved=False)

    assert response.status_code == 200
    assert media.retained == []
    assert container.of_type("media_retention_work_item") == []
    assert "ttl" in _mapping_document(container)  # still an orphan, cleared in 24 hours


def test_a_failed_move_keeps_the_reply_and_the_work_item(monkeypatch):
    container, approvals, card = _photo_card(monkeypatch)
    media = Media(error=ConnectionError("Blob storage is unreachable"))

    with TestClient(_app(container, approvals, media)) as client:
        response = _resume(client, card)

    assert response.status_code == 200
    assert response.json()["message"] == "已記錄：衛生紙 +2 包（現在 3 包）"
    assert media.retained == [(HOME, PHOTO)]
    item = _work_item(container)
    assert item["status"] == "pending"  # retried at the next confirm in this household
    assert item["attempt_count"] == 1


class _Recording(MediaRetentionPostCommitTrigger):
    """Records each start instead of running the worker."""

    def __init__(self, calls: list) -> None:
        self.calls = calls

    async def after_resume(self, *, household_id, result):
        self.calls.append((household_id, result.outcome.value))


def _recording_app(approvals, calls):
    app = resume_app(approvals)
    trigger = _Recording(calls)
    app.dependency_overrides[get_media_retention_trigger] = lambda: trigger
    return app


def test_a_cancelled_card_does_not_start_the_trigger(monkeypatch):
    container, approvals, card = _photo_card(monkeypatch)
    calls = []

    with TestClient(_recording_app(approvals, calls)) as client:
        _resume(client, card, approved=False)

    assert calls == []


def test_only_the_first_write_starts_the_trigger(monkeypatch):
    """A double tap does not start a second run of the same work (PR #203 A2 review)."""
    container, approvals, card = _photo_card(monkeypatch)
    calls = []

    with TestClient(_recording_app(approvals, calls)) as client:
        first = _resume(client, card)
        again = _resume(client, card)

    assert again.json()["message"] == first.json()["message"]
    assert calls == [(HOME, "completed")]


def test_without_cosmos_there_is_no_trigger():
    assert _create_media_retention_trigger(None, None, Media()) is None


def test_the_trigger_singleton_can_be_reset_for_tests():
    dependencies._media_retention_trigger = object()
    dependencies._media_retention_trigger_resolved = True

    _reset_media_retention_trigger_for_test()

    assert dependencies._media_retention_trigger is None
    assert dependencies._media_retention_trigger_resolved is False


def test_the_trigger_uses_the_approval_services_cosmos_client():
    container = Container()
    writes = CosmosWriteRepository(container, query_client=container)
    media = Media()

    trigger = _create_media_retention_trigger(writes, container, media)

    worker = trigger._worker
    storage = worker._store._storage
    assert storage._write_client is writes.transactional_batch_client
    assert storage._query_client is container
    assert worker._media is media

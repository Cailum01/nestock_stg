from datetime import UTC, datetime, timedelta

import pytest
from azure.core.exceptions import HttpResponseError, ServiceRequestError
from test_shopping_workflow import run

from app.services.media_retention_work import (
    MediaRetentionWorkItem as B1WorkRecord,
)
from app.services.media_retention_work import (
    MediaRetentionWorkItemSnapshot,
    MediaRetentionWorkStatus,
)
from app.services.provisional_storage.error_classifier import (
    DEFAULT_CONNECTION_BACKOFF_SECONDS,
    DEFAULT_SERVICE_UNAVAILABLE_BACKOFF_SECONDS,
)
from app.services.shopping.media_handoff import (
    B1MediaRetentionWorkItemAdapter,
    MediaRetentionRetryWorker,
    MediaRetentionWorkItem,
    NewMediaRetentionWorkItem,
    media_retention_work_item_id,
)

HOUSEHOLD = "household-a"
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


class FakeWorkItemStore:
    def __init__(self, items):
        self.items = items
        self.completed = []
        self.retries = []
        self.manual = []

    async def list_due_pending(self, *, household_id, due_at):
        return [
            item
            for item in self.items
            if item.household_id == household_id
            and (item.next_attempt_at is None or item.next_attempt_at <= due_at)
        ]

    async def mark_completed(self, item, *, updated_at):
        self.completed.append((item, updated_at))

    async def schedule_retry(self, item, *, attempt_count, next_attempt_at, error_code, updated_at):
        self.retries.append((item, attempt_count, next_attempt_at, error_code, updated_at))

    async def mark_needs_attention(self, item, *, error_code, updated_at):
        self.manual.append((item, error_code, updated_at))


class FakeMedia:
    def __init__(self, result=True, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def retain_media(self, household_id, image_ref):
        self.calls.append((household_id, image_ref))
        if self.error:
            raise self.error
        return self.result


def http_failure(status_code):
    error = HttpResponseError(message=f"HTTP {status_code}")
    error.status_code = status_code
    return error


class FakeB1WorkStorage:
    """Implements the merged #191 port shape, including ETag snapshots."""

    def __init__(self, snapshots=()):
        self.snapshots = list(snapshots)
        self.queries = []
        self.transitions = []

    async def list_due(self, *, household_id, now, limit):
        self.queries.append((household_id, now, limit))
        return self.snapshots

    async def transition(self, snapshot, **changes):
        self.transitions.append((snapshot, changes))
        return snapshot


def b1_snapshot():
    return MediaRetentionWorkItemSnapshot(
        record=B1WorkRecord(
            household_id=HOUSEHOLD,
            operation_id="purchase-op-1",
            approval_id="approval-1",
            image_ref="opaque-image-ref",
            status=MediaRetentionWorkStatus.PENDING,
            attempt_count=0,
            next_attempt_at=NOW,
            last_error_code=None,
            created_at=NOW,
            updated_at=NOW,
        ),
        etag="etag-b1-1",
    )


def test_b1_adapter_builds_initial_item_with_canonical_port_and_identity():
    storage = FakeB1WorkStorage()
    adapter = B1MediaRetentionWorkItemAdapter(storage)
    draft = NewMediaRetentionWorkItem(
        id=media_retention_work_item_id("purchase-op-1"),
        household_id=HOUSEHOLD,
        operation_id="purchase-op-1",
        approval_id="approval-1",
        image_ref="opaque-image-ref",
        created_at=NOW,
        updated_at=NOW,
    )

    operation = adapter.create_pending(draft)

    assert operation.document["id"] == draft.id
    assert operation.document["record_type"] == "media_retention_work_item"
    assert operation.document["household_id"] == HOUSEHOLD
    assert operation.document["operation_id"] == "purchase-op-1"
    assert operation.document["approval_id"] == "approval-1"
    assert operation.document["image_ref"] == "opaque-image-ref"
    assert operation.document["status"] == "pending"
    assert operation.document["next_attempt_at"] == "2026-11-17T12:00:00.000000Z"


def test_b1_adapter_uses_formal_due_query_and_etag_snapshot_transition():
    snapshot = b1_snapshot()
    storage = FakeB1WorkStorage([snapshot])
    adapter = B1MediaRetentionWorkItemAdapter(storage, query_limit=8)
    media = FakeMedia()

    result = run(
        MediaRetentionRetryWorker(adapter, media).process_pending(household_id=HOUSEHOLD, now=NOW)
    )

    assert storage.queries == [(HOUSEHOLD, NOW, 8)]
    assert media.calls == [(HOUSEHOLD, "opaque-image-ref")]
    sent_snapshot, changes = storage.transitions[0]
    assert sent_snapshot is snapshot
    assert changes == {
        "status": MediaRetentionWorkStatus.COMPLETED,
        "attempt_count": 0,
        "next_attempt_at": None,
        "last_error_code": None,
        "updated_at": NOW,
    }
    assert result.completed == 1


def item(*, attempt_count=0, next_attempt_at=None, household_id=HOUSEHOLD):
    return MediaRetentionWorkItem(
        id=media_retention_work_item_id("purchase-op-1"),
        household_id=household_id,
        operation_id="purchase-op-1",
        approval_id="approval-1",
        image_ref="opaque-image-ref",
        etag="etag-1",
        attempt_count=attempt_count,
        next_attempt_at=next_attempt_at,
    )


def test_success_marks_work_item_complete_after_idempotent_retain():
    work = item()
    store, media = FakeWorkItemStore([work]), FakeMedia()
    worker = MediaRetentionRetryWorker(store, media)

    result = run(worker.process_pending(household_id=HOUSEHOLD, now=NOW))

    assert media.calls == [(HOUSEHOLD, "opaque-image-ref")]
    assert store.completed == [(work, NOW)]
    assert store.retries == []
    assert store.manual == []
    assert result.completed == 1


def test_not_due_item_is_left_untouched():
    work = item(next_attempt_at=NOW + timedelta(seconds=1))
    store, media = FakeWorkItemStore([work]), FakeMedia()

    result = run(
        MediaRetentionRetryWorker(store, media).process_pending(household_id=HOUSEHOLD, now=NOW)
    )

    assert media.calls == []
    assert store.completed == store.retries == store.manual == []
    assert result.completed == result.retry_scheduled == result.needs_attention == 0


@pytest.mark.parametrize("status_code", [502, 503, 504])
def test_transient_http_errors_use_a2_backoff_and_code(status_code):
    work = item(attempt_count=1)
    store, media = FakeWorkItemStore([work]), FakeMedia(error=http_failure(status_code))
    worker = MediaRetentionRetryWorker(store, media, jitter=lambda: 0.1)

    result = run(worker.process_pending(household_id=HOUSEHOLD, now=NOW))

    assert store.retries == [
        (
            work,
            2,
            NOW + timedelta(seconds=DEFAULT_SERVICE_UNAVAILABLE_BACKOFF_SECONDS * 1.1),
            "service_unavailable",
            NOW,
        )
    ]
    assert store.manual == []
    assert result.retry_scheduled == 1


def test_service_request_error_uses_a2_connection_backoff():
    work = item()
    store, media = FakeWorkItemStore([work]), FakeMedia(error=ServiceRequestError("offline"))

    result = run(
        MediaRetentionRetryWorker(store, media, jitter=lambda: 0.0).process_pending(
            household_id=HOUSEHOLD, now=NOW
        )
    )

    assert store.retries == [
        (
            work,
            1,
            NOW + timedelta(seconds=DEFAULT_CONNECTION_BACKOFF_SECONDS),
            "network_connection_error",
            NOW,
        )
    ]
    assert store.manual == []
    assert result.retry_scheduled == 1


def test_permanent_error_moves_work_item_to_needs_attention():
    work = item()
    store, media = FakeWorkItemStore([work]), FakeMedia(error=http_failure(404))

    result = run(
        MediaRetentionRetryWorker(store, media).process_pending(household_id=HOUSEHOLD, now=NOW)
    )

    assert store.manual == [(work, "blob_not_found", NOW)]
    assert store.retries == []
    assert result.needs_attention == 1


def test_false_result_needs_attention_and_never_retries():
    work = item()
    store, media = FakeWorkItemStore([work]), FakeMedia(result=False)

    result = run(
        MediaRetentionRetryWorker(store, media).process_pending(household_id=HOUSEHOLD, now=NOW)
    )

    assert store.manual == [(work, "media_not_retainable", NOW)]
    assert store.retries == []
    assert result.needs_attention == 1


def test_exhausted_transient_retries_require_attention():
    work = item(attempt_count=3)
    store, media = FakeWorkItemStore([work]), FakeMedia(error=TimeoutError())

    result = run(
        MediaRetentionRetryWorker(store, media).process_pending(household_id=HOUSEHOLD, now=NOW)
    )

    assert store.manual == [(work, "transient_retries_exhausted", NOW)]
    assert store.retries == []
    assert result.needs_attention == 1


def test_wrong_household_work_item_fails_closed_before_blob_call():
    class InvalidStore(FakeWorkItemStore):
        async def list_due_pending(self, *, household_id, due_at):
            return self.items

    store, media = InvalidStore([item(household_id="household-b")]), FakeMedia()

    with pytest.raises(ValueError, match="different household"):
        run(
            MediaRetentionRetryWorker(store, media).process_pending(household_id=HOUSEHOLD, now=NOW)
        )

    assert media.calls == []


def test_unknown_exception_is_manual_action_by_default():
    work = item()
    store, media = FakeWorkItemStore([work]), FakeMedia(error=RuntimeError("unexpected"))

    run(MediaRetentionRetryWorker(store, media).process_pending(household_id=HOUSEHOLD, now=NOW))

    assert store.manual == [(work, "storage_fatal_error", NOW)]
    assert store.retries == []

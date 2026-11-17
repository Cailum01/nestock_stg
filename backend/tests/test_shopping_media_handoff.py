from datetime import UTC, datetime, timedelta

import pytest
from test_shopping_workflow import run

from app.services.shopping.media_handoff import (
    MediaRetentionRetryWorker,
    MediaRetentionWorkItem,
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


class HttpFailure(Exception):
    def __init__(self, status_code):
        self.status_code = status_code


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


def test_transient_error_persists_attempt_count_and_jittered_due_time():
    work = item(attempt_count=1)
    store, media = FakeWorkItemStore([work]), FakeMedia(error=HttpFailure(503))
    worker = MediaRetentionRetryWorker(store, media, jitter=lambda: 0.0)

    result = run(worker.process_pending(household_id=HOUSEHOLD, now=NOW))

    assert store.retries == [
        (work, 2, NOW + timedelta(seconds=2), "transient_storage_failure", NOW)
    ]
    assert store.manual == []
    assert result.retry_scheduled == 1


def test_permanent_error_moves_work_item_to_needs_attention():
    work = item()
    store, media = FakeWorkItemStore([work]), FakeMedia(error=HttpFailure(404))

    result = run(
        MediaRetentionRetryWorker(store, media).process_pending(household_id=HOUSEHOLD, now=NOW)
    )

    assert store.manual == [(work, "media_not_found", NOW)]
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

    assert store.manual == [(work, "permanent_retention_failure", NOW)]
    assert store.retries == []

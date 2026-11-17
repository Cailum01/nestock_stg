"""C1 retry orchestration for committed purchase media-retention work (Issue #98).

The durable work-item storage is supplied by B1 and the physical retention operation
by A2. This module owns only the orchestration policy; it does not access Cosmos
documents or change purchase approvals. A completed work item is safe to process again
because A2's ``retain_media`` contract is idempotent.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Protocol

from app.services.cosmos_write import CreateDocument
from app.services.media_retention_work import (
    MediaRetentionWorkItemSnapshot,
    MediaRetentionWorkItemStorage,
    MediaRetentionWorkStatus,
    build_pending_media_retention_work_item,
)
from app.services.provisional_storage import classify_media_retention_error


@dataclass(frozen=True)
class NewMediaRetentionWorkItem:
    """Fixed values C1 asks B1 to persist atomically with the purchase."""

    id: str
    household_id: str
    operation_id: str
    approval_id: str
    image_ref: str
    created_at: datetime
    updated_at: datetime


class MediaRetentionWorkItemWriter(Protocol):
    """Narrow C1 purchase-batch port implemented by the B1 helper adapter."""

    def create_pending(self, draft: NewMediaRetentionWorkItem) -> CreateDocument: ...


@dataclass(frozen=True)
class MediaRetentionWorkItem:
    """C1 projection of B1's household-scoped durable work item."""

    id: str
    household_id: str
    operation_id: str
    approval_id: str
    image_ref: str
    etag: str
    attempt_count: int = 0
    next_attempt_at: datetime | None = None
    snapshot: MediaRetentionWorkItemSnapshot | None = None

    def __post_init__(self) -> None:
        identity = (
            self.id,
            self.household_id,
            self.operation_id,
            self.approval_id,
            self.image_ref,
            self.etag,
        )
        if not all(isinstance(value, str) and value.strip() for value in identity):
            raise ValueError("Work-item and media identities must not be empty.")
        if (
            isinstance(self.attempt_count, bool)
            or not isinstance(self.attempt_count, int)
            or self.attempt_count < 0
        ):
            raise ValueError("attempt_count must be a non-negative integer.")
        if self.next_attempt_at is not None and self.next_attempt_at.tzinfo is None:
            raise ValueError("next_attempt_at must be timezone-aware.")


class MediaRetentionWorkItemStore(Protocol):
    """B1 port; every state update must use the item's ETag in its household partition."""

    async def list_due_pending(
        self, *, household_id: str, due_at: datetime
    ) -> Sequence[MediaRetentionWorkItem]: ...

    async def mark_completed(
        self, item: MediaRetentionWorkItem, *, updated_at: datetime
    ) -> None: ...

    async def schedule_retry(
        self,
        item: MediaRetentionWorkItem,
        *,
        attempt_count: int,
        next_attempt_at: datetime,
        error_code: str,
        updated_at: datetime,
    ) -> None: ...

    async def mark_needs_attention(
        self, item: MediaRetentionWorkItem, *, error_code: str, updated_at: datetime
    ) -> None: ...


class B1MediaRetentionWorkItemAdapter:
    """Map C1's purchase/worker needs onto B1's merged #191 storage port."""

    def __init__(self, storage: MediaRetentionWorkItemStorage, *, query_limit: int = 100):
        if (
            isinstance(query_limit, bool)
            or not isinstance(query_limit, int)
            or not 1 <= query_limit <= 100
        ):
            raise ValueError("query_limit must be between 1 and 100.")
        self._storage = storage
        self._query_limit = query_limit

    def create_pending(self, draft: NewMediaRetentionWorkItem) -> CreateDocument:
        """Build the durable pending record with B1's canonical schema and ID."""
        if draft.created_at != draft.updated_at:
            raise ValueError("Initial work-item timestamps must match the approval time.")
        return build_pending_media_retention_work_item(
            household_id=draft.household_id,
            operation_id=draft.operation_id,
            approval_id=draft.approval_id,
            image_ref=draft.image_ref,
            created_at=draft.created_at,
        )

    async def list_due_pending(
        self, *, household_id: str, due_at: datetime
    ) -> Sequence[MediaRetentionWorkItem]:
        snapshots = await self._storage.list_due(
            household_id=household_id, now=due_at, limit=self._query_limit
        )
        return tuple(self._project(snapshot) for snapshot in snapshots)

    async def mark_completed(self, item: MediaRetentionWorkItem, *, updated_at: datetime) -> None:
        await self._transition(
            item,
            status=MediaRetentionWorkStatus.COMPLETED,
            attempt_count=item.attempt_count,
            next_attempt_at=None,
            last_error_code=None,
            updated_at=updated_at,
        )

    async def schedule_retry(
        self,
        item: MediaRetentionWorkItem,
        *,
        attempt_count: int,
        next_attempt_at: datetime,
        error_code: str,
        updated_at: datetime,
    ) -> None:
        await self._transition(
            item,
            status=MediaRetentionWorkStatus.PENDING,
            attempt_count=attempt_count,
            next_attempt_at=next_attempt_at,
            last_error_code=error_code,
            updated_at=updated_at,
        )

    async def mark_needs_attention(
        self, item: MediaRetentionWorkItem, *, error_code: str, updated_at: datetime
    ) -> None:
        await self._transition(
            item,
            status=MediaRetentionWorkStatus.NEEDS_ATTENTION,
            attempt_count=item.attempt_count,
            next_attempt_at=None,
            last_error_code=error_code,
            updated_at=updated_at,
        )

    @staticmethod
    def _project(snapshot: MediaRetentionWorkItemSnapshot) -> MediaRetentionWorkItem:
        record = snapshot.record
        return MediaRetentionWorkItem(
            id=record.document_id,
            household_id=record.household_id,
            operation_id=record.operation_id,
            approval_id=record.approval_id,
            image_ref=record.image_ref,
            etag=snapshot.etag,
            attempt_count=record.attempt_count,
            next_attempt_at=record.next_attempt_at,
            snapshot=snapshot,
        )

    async def _transition(self, item: MediaRetentionWorkItem, **changes) -> None:
        if item.snapshot is None:
            raise ValueError("A B1 ETag snapshot is required for a work-item transition.")
        await self._storage.transition(item.snapshot, **changes)


class MediaRetentionPort(Protocol):
    """A2's idempotent Blob handoff operation."""

    async def retain_media(self, household_id: str, image_ref: str) -> bool: ...


@dataclass(frozen=True)
class MediaRetentionRun:
    completed: int = 0
    retry_scheduled: int = 0
    needs_attention: int = 0


class MediaRetentionRetryWorker:
    """Process committed work items without replaying their purchase writes.

    A retry means another call to A2's idempotent ``retain_media`` only. The initial
    attempt is made by the caller after the purchase batch commits; this worker permits
    three further attempts using A2's classified backoff recommendation, with jitter. Transient
    errors remain durable and pending; permanent errors and exhausted retries are
    retained in ``needs_attention`` for manual handling.
    """

    _MAX_RETRIES = 3

    def __init__(
        self,
        store: MediaRetentionWorkItemStore,
        media: MediaRetentionPort,
        *,
        jitter: Callable[[], float] | None = None,
    ) -> None:
        self._store = store
        self._media = media
        self._jitter = jitter or (lambda: random.uniform(-0.25, 0.25))

    async def process_pending(
        self, *, household_id: str, now: datetime | None = None
    ) -> MediaRetentionRun:
        """Process due pending items for one household only."""
        effective_now = now or datetime.now(UTC)
        completed = retry_scheduled = needs_attention = 0
        for item in await self._store.list_due_pending(
            household_id=household_id, due_at=effective_now
        ):
            if item.household_id != household_id:
                raise ValueError("Work-item store returned a different household.")
            try:
                retained = await self._media.retain_media(item.household_id, item.image_ref)
            except Exception as error:
                classification = classify_media_retention_error(error)
                if classification.is_transient:
                    scheduled = await self._schedule_retry(
                        item,
                        effective_now,
                        backoff_seconds=classification.suggested_backoff_seconds,
                        error_code=classification.error_code,
                    )
                    if scheduled:
                        retry_scheduled += 1
                    else:
                        needs_attention += 1
                else:
                    await self._store.mark_needs_attention(
                        item, error_code=classification.error_code, updated_at=effective_now
                    )
                    needs_attention += 1
            else:
                if retained:
                    await self._store.mark_completed(item, updated_at=effective_now)
                    completed += 1
                else:
                    # A2 returns False for missing or already-purged media. It cannot
                    # be safely recreated as part of the completed purchase.
                    await self._store.mark_needs_attention(
                        item,
                        error_code="media_not_retainable",
                        updated_at=effective_now,
                    )
                    needs_attention += 1
        return MediaRetentionRun(
            completed=completed,
            retry_scheduled=retry_scheduled,
            needs_attention=needs_attention,
        )

    async def _schedule_retry(
        self,
        item: MediaRetentionWorkItem,
        now: datetime,
        *,
        backoff_seconds: float,
        error_code: str,
    ) -> bool:
        attempt_count = item.attempt_count + 1
        if attempt_count > self._MAX_RETRIES:
            await self._store.mark_needs_attention(
                item, error_code="transient_retries_exhausted", updated_at=now
            )
            return False
        jitter = self._jitter()
        if not -0.25 <= jitter <= 0.25:
            raise ValueError("jitter must be between -0.25 and 0.25.")
        delay = backoff_seconds * (1 + jitter)
        await self._store.schedule_retry(
            item,
            attempt_count=attempt_count,
            next_attempt_at=now + timedelta(seconds=delay),
            error_code=error_code,
            updated_at=now,
        )
        return True


def media_retention_work_item_id(operation_id: str) -> str:
    """B1 contract: stable SHA-256-derived ID, scoped by household partition."""
    digest = sha256(operation_id.encode("utf-8")).hexdigest()
    return f"media_retention:{digest}"

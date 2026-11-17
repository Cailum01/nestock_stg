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
    """B1 port that maps C1's fixed draft to a same-partition CreateDocument."""

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
    three further attempts at approximately 1, 2 and 4 seconds, with jitter. Transient
    errors remain durable and pending; permanent errors and exhausted retries are
    retained in ``needs_attention`` for manual handling.
    """

    _TRANSIENT_STATUS_CODES = frozenset({429, 500, 503})
    _RETRY_DELAYS_SECONDS = (1.0, 2.0, 4.0)

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
                if self._is_transient(error):
                    scheduled = await self._schedule_retry(item, effective_now)
                    if scheduled:
                        retry_scheduled += 1
                    else:
                        needs_attention += 1
                else:
                    await self._store.mark_needs_attention(
                        item, error_code=self._error_code(error), updated_at=effective_now
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

    async def _schedule_retry(self, item: MediaRetentionWorkItem, now: datetime) -> bool:
        attempt_count = item.attempt_count + 1
        if attempt_count > len(self._RETRY_DELAYS_SECONDS):
            await self._store.mark_needs_attention(
                item, error_code="transient_retries_exhausted", updated_at=now
            )
            return False
        jitter = self._jitter()
        if not -0.25 <= jitter <= 0.25:
            raise ValueError("jitter must be between -0.25 and 0.25.")
        delay = self._RETRY_DELAYS_SECONDS[attempt_count - 1] * (1 + jitter)
        await self._store.schedule_retry(
            item,
            attempt_count=attempt_count,
            next_attempt_at=now + timedelta(seconds=delay),
            error_code="transient_storage_failure",
            updated_at=now,
        )
        return True

    @classmethod
    def _is_transient(cls, error: Exception) -> bool:
        if isinstance(error, (TimeoutError, ConnectionError)):
            return True
        status_code = getattr(error, "status_code", None)
        return status_code in cls._TRANSIENT_STATUS_CODES

    @staticmethod
    def _error_code(error: Exception) -> str:
        status_code = getattr(error, "status_code", None)
        if status_code == 404:
            return "media_not_found"
        return "permanent_retention_failure"


def media_retention_work_item_id(operation_id: str) -> str:
    """B1 contract: stable SHA-256-derived ID, scoped by household partition."""
    digest = sha256(operation_id.encode("utf-8")).hexdigest()
    return f"media_retention:{digest}"

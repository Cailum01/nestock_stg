"""B1's durable storage port for post-commit media retention work (Issue #98)."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from typing import Protocol

from app.services.cosmos_read import DocumentQueryClient
from app.services.cosmos_write import (
    BatchOperationConflict,
    CreateDocument,
    ReplaceDocument,
    TransactionalBatchClient,
)

_RECORD_TYPE = "media_retention_work_item"
_MAX_QUERY_LIMIT = 100
_ERROR_CODE = re.compile(r"[a-z0-9_]{1,64}\Z")


class MediaRetentionWorkStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    NEEDS_ATTENTION = "needs_attention"


class MediaRetentionWorkItemNotFound(Exception):
    """The household-scoped media-retention work item does not exist."""


class MediaRetentionWorkItemConflict(Exception):
    """The work item cannot make the requested state transition."""


@dataclass(frozen=True)
class MediaRetentionWorkItem:
    household_id: str
    operation_id: str
    approval_id: str
    image_ref: str
    status: MediaRetentionWorkStatus
    attempt_count: int
    next_attempt_at: datetime | None
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime

    @property
    def document_id(self) -> str:
        digest = hashlib.sha256(self.operation_id.encode("utf-8")).hexdigest()
        return f"media_retention:{digest}"


@dataclass(frozen=True)
class MediaRetentionWorkItemSnapshot:
    record: MediaRetentionWorkItem
    etag: str


class MediaRetentionWorkItemStorage(Protocol):
    """Household-scoped read and ETag-guarded update port for C1's retry worker."""

    async def get(
        self, *, household_id: str, operation_id: str
    ) -> MediaRetentionWorkItemSnapshot | None: ...

    async def list_due(
        self, *, household_id: str, now: datetime, limit: int
    ) -> list[MediaRetentionWorkItemSnapshot]: ...

    async def transition(
        self,
        snapshot: MediaRetentionWorkItemSnapshot,
        *,
        status: MediaRetentionWorkStatus,
        attempt_count: int,
        next_attempt_at: datetime | None,
        last_error_code: str | None,
        updated_at: datetime,
    ) -> MediaRetentionWorkItemSnapshot: ...


def build_pending_media_retention_work_item(
    *,
    household_id: str,
    operation_id: str,
    approval_id: str,
    image_ref: str,
    created_at: datetime,
) -> CreateDocument:
    """Build the pending item for inclusion in the purchase's household batch."""
    for value, label in (
        (household_id, "household_id"),
        (operation_id, "operation_id"),
        (approval_id, "approval_id"),
        (image_ref, "image_ref"),
    ):
        _require_text(value, label)
    _require_aware(created_at, "created_at")

    record = MediaRetentionWorkItem(
        household_id=household_id,
        operation_id=operation_id,
        approval_id=approval_id,
        image_ref=image_ref,
        status=MediaRetentionWorkStatus.PENDING,
        attempt_count=0,
        next_attempt_at=created_at,
        last_error_code=None,
        created_at=created_at,
        updated_at=created_at,
    )
    return CreateDocument(_to_document(record))


class CosmosMediaRetentionWorkItemStorage:
    """Offline-testable media-retention work storage over one household container."""

    def __init__(
        self,
        *,
        write_client: TransactionalBatchClient,
        query_client: DocumentQueryClient,
    ) -> None:
        self._write_client = write_client
        self._query_client = query_client

    async def get(
        self, *, household_id: str, operation_id: str
    ) -> MediaRetentionWorkItemSnapshot | None:
        _require_text(household_id, "household_id")
        _require_text(operation_id, "operation_id")
        document = await self._write_client.read_item(
            item_id=_media_retention_document_id(operation_id),
            partition_key=household_id,
        )
        return None if document is None else _snapshot(document, household_id=household_id)

    async def list_due(
        self, *, household_id: str, now: datetime, limit: int
    ) -> list[MediaRetentionWorkItemSnapshot]:
        _require_text(household_id, "household_id")
        _require_aware(now, "now")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= _MAX_QUERY_LIMIT
        ):
            raise ValueError(f"limit must be an integer between 1 and {_MAX_QUERY_LIMIT}.")

        documents = await self._query_client.query_items(
            query=(
                "SELECT TOP @limit * FROM c "
                "WHERE c.record_type = @record_type "
                "AND c.household_id = @household_id "
                "AND c.status = @status "
                "AND c.next_attempt_at <= @now "
                "ORDER BY c.next_attempt_at ASC, c.created_at ASC"
            ),
            parameters=(
                {"name": "@limit", "value": limit},
                {"name": "@record_type", "value": _RECORD_TYPE},
                {"name": "@household_id", "value": household_id},
                {"name": "@status", "value": MediaRetentionWorkStatus.PENDING.value},
                {"name": "@now", "value": _utc_isoformat(now)},
            ),
            partition_key=household_id,
        )
        snapshots = [_snapshot(document, household_id=household_id) for document in documents]
        if any(
            snapshot.record.status is not MediaRetentionWorkStatus.PENDING
            or snapshot.record.next_attempt_at is None
            or snapshot.record.next_attempt_at > now
            for snapshot in snapshots
        ):
            raise ValueError("Due work query returned an item that is not due and pending.")
        return snapshots

    async def transition(
        self,
        snapshot: MediaRetentionWorkItemSnapshot,
        *,
        status: MediaRetentionWorkStatus,
        attempt_count: int,
        next_attempt_at: datetime | None,
        last_error_code: str | None,
        updated_at: datetime,
    ) -> MediaRetentionWorkItemSnapshot:
        current_snapshot = await self.get(
            household_id=snapshot.record.household_id,
            operation_id=snapshot.record.operation_id,
        )
        if current_snapshot is None:
            raise MediaRetentionWorkItemNotFound(snapshot.record.operation_id)

        try:
            status = MediaRetentionWorkStatus(status)
        except ValueError as exc:
            raise ValueError("Unknown media-retention work status.") from exc
        updated = _updated_record(
            snapshot.record,
            status=status,
            attempt_count=attempt_count,
            next_attempt_at=next_attempt_at,
            last_error_code=last_error_code,
            updated_at=updated_at,
        )
        if current_snapshot.record == updated:
            return current_snapshot
        if current_snapshot.etag != snapshot.etag or current_snapshot.record != snapshot.record:
            raise BatchOperationConflict(0)

        try:
            await self._write_client.execute_batch(
                partition_key=updated.household_id,
                operations=(
                    ReplaceDocument(
                        document=_to_document(updated),
                        if_match_etag=snapshot.etag,
                    ),
                ),
            )
        except BatchOperationConflict:
            saved = await self.get(
                household_id=updated.household_id,
                operation_id=updated.operation_id,
            )
            if saved is not None and saved.record == updated:
                return saved
            raise

        saved = await self.get(
            household_id=updated.household_id,
            operation_id=updated.operation_id,
        )
        if saved is None:
            raise RuntimeError("Updated media-retention work item could not be read back.")
        if saved.record != updated:
            raise RuntimeError("Updated media-retention work item did not match the request.")
        return saved


def _updated_record(
    current: MediaRetentionWorkItem,
    *,
    status: MediaRetentionWorkStatus,
    attempt_count: int,
    next_attempt_at: datetime | None,
    last_error_code: str | None,
    updated_at: datetime,
) -> MediaRetentionWorkItem:
    if current.status is not MediaRetentionWorkStatus.PENDING:
        raise MediaRetentionWorkItemConflict(
            f"Cannot transition a {current.status.value} work item."
        )
    if (
        isinstance(attempt_count, bool)
        or not isinstance(attempt_count, int)
        or attempt_count < current.attempt_count
    ):
        raise ValueError("attempt_count must be an integer no lower than its current value.")
    _require_aware(updated_at, "updated_at")
    if updated_at < current.updated_at:
        raise ValueError("updated_at cannot move backwards.")
    if next_attempt_at is not None:
        _require_aware(next_attempt_at, "next_attempt_at")
    if last_error_code is not None and (
        not isinstance(last_error_code, str) or not _ERROR_CODE.fullmatch(last_error_code)
    ):
        raise ValueError("last_error_code must be a short lowercase code, not an error message.")

    if status is MediaRetentionWorkStatus.PENDING:
        if next_attempt_at is None:
            raise ValueError("A pending work item requires next_attempt_at.")
    elif status is MediaRetentionWorkStatus.COMPLETED:
        if next_attempt_at is not None or last_error_code is not None:
            raise ValueError("A completed work item cannot have a retry time or error code.")
    elif status is MediaRetentionWorkStatus.NEEDS_ATTENTION:
        if next_attempt_at is not None or last_error_code is None:
            raise ValueError("A work item needing attention requires an error and no retry time.")

    return replace(
        current,
        status=status,
        attempt_count=attempt_count,
        next_attempt_at=next_attempt_at,
        last_error_code=last_error_code,
        updated_at=updated_at,
    )


def _snapshot(
    document: Mapping[str, object], *, household_id: str
) -> MediaRetentionWorkItemSnapshot:
    operation_id = document.get("operation_id")
    approval_id = document.get("approval_id")
    image_ref = document.get("image_ref")
    status = document.get("status")
    attempt_count = document.get("attempt_count")
    last_error_code = document.get("last_error_code")
    etag = document.get("_etag")
    if (
        document.get("record_type") != _RECORD_TYPE
        or document.get("household_id") != household_id
        or not isinstance(operation_id, str)
        or not operation_id.strip()
        or not isinstance(approval_id, str)
        or not approval_id.strip()
        or not isinstance(image_ref, str)
        or not image_ref.strip()
        or isinstance(attempt_count, bool)
        or not isinstance(attempt_count, int)
        or attempt_count < 0
        or not isinstance(etag, str)
        or not etag
        or (last_error_code is not None and not isinstance(last_error_code, str))
    ):
        raise ValueError("Stored media-retention work item has invalid identity or metadata.")
    if document.get("id") != _media_retention_document_id(operation_id):
        raise ValueError("Stored media-retention work item has an invalid document ID.")
    try:
        parsed_status = MediaRetentionWorkStatus(status)
    except (TypeError, ValueError) as exc:
        raise ValueError("Stored media-retention work item has an invalid status.") from exc

    record = MediaRetentionWorkItem(
        household_id=household_id,
        operation_id=operation_id,
        approval_id=approval_id,
        image_ref=image_ref,
        status=parsed_status,
        attempt_count=attempt_count,
        next_attempt_at=_parse_optional_timestamp(document.get("next_attempt_at")),
        last_error_code=last_error_code,
        created_at=_parse_timestamp(document.get("created_at"), "created_at"),
        updated_at=_parse_timestamp(document.get("updated_at"), "updated_at"),
    )
    if record.updated_at < record.created_at:
        raise ValueError("Stored work item updated_at cannot precede created_at.")
    _validate_record_state(record)
    return MediaRetentionWorkItemSnapshot(record=record, etag=etag)


def _validate_record_state(record: MediaRetentionWorkItem) -> None:
    if record.last_error_code is not None and (
        not isinstance(record.last_error_code, str)
        or not _ERROR_CODE.fullmatch(record.last_error_code)
    ):
        raise ValueError("Stored last_error_code is not a valid error code.")
    if record.status is MediaRetentionWorkStatus.PENDING:
        if record.next_attempt_at is None:
            raise ValueError("Pending work item is missing next_attempt_at.")
    elif record.status is MediaRetentionWorkStatus.COMPLETED:
        if record.next_attempt_at is not None or record.last_error_code is not None:
            raise ValueError("Completed work item has retry metadata.")
    elif record.next_attempt_at is not None or record.last_error_code is None:
        raise ValueError("Work item needing attention has invalid retry metadata.")


def _to_document(record: MediaRetentionWorkItem) -> dict[str, object]:
    _validate_record_state(record)
    return {
        "id": record.document_id,
        "record_type": _RECORD_TYPE,
        "household_id": record.household_id,
        "operation_id": record.operation_id,
        "approval_id": record.approval_id,
        "image_ref": record.image_ref,
        "status": record.status.value,
        "attempt_count": record.attempt_count,
        "next_attempt_at": (
            None if record.next_attempt_at is None else _utc_isoformat(record.next_attempt_at)
        ),
        "last_error_code": record.last_error_code,
        "created_at": _utc_isoformat(record.created_at),
        "updated_at": _utc_isoformat(record.updated_at),
    }


def _media_retention_document_id(operation_id: object) -> str:
    if not isinstance(operation_id, str) or not operation_id:
        raise ValueError("operation_id must not be empty.")
    return f"media_retention:{hashlib.sha256(operation_id.encode('utf-8')).hexdigest()}"


def _parse_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"Stored {field} must be a timestamp string.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Stored {field} must be an ISO timestamp.") from exc
    _require_aware(parsed, field)
    return parsed.astimezone(timezone.utc)


def _parse_optional_timestamp(value: object) -> datetime | None:
    return None if value is None else _parse_timestamp(value, "next_attempt_at")


def _require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware.")


def _require_text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must not be empty.")


def _utc_isoformat(value: datetime) -> str:
    _require_aware(value, "timestamp")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

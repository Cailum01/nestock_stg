"""B1's provisional household-scoped media-mapping storage port."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.models.domain import (
    MediaFailureReason,
    MediaInputMapping,
    MediaProcessingStatus,
)
from app.services.cosmos_read import (
    CosmosDocumentMapper,
    CosmosReadRepository,
    MappedDocument,
)
from app.services.cosmos_write import (
    BatchOperationConflict,
    CreateDocument,
    ReplaceDocument,
    TransactionalBatchClient,
)

_ALLOWED_TRANSITIONS = {
    MediaProcessingStatus.ALLOCATED: {
        MediaProcessingStatus.RECEIVED,
        MediaProcessingStatus.FAILED,
    },
    MediaProcessingStatus.RECEIVED: {
        MediaProcessingStatus.PROCESSING,
        MediaProcessingStatus.FAILED,
    },
    MediaProcessingStatus.PROCESSING: {
        MediaProcessingStatus.COMPLETED,
        MediaProcessingStatus.FAILED,
    },
}


class MediaMappingError(Exception):
    """Base error for media-mapping persistence and lifecycle failures."""


class MediaMappingNotFound(MediaMappingError):
    """The household-scoped media mapping does not exist."""


class MediaMappingNotReady(MediaMappingError):
    """The mapping exists but its media is not ready for Agent use."""


class MediaMappingConflict(MediaMappingError):
    """The mapping identity or state conflicts with the requested operation."""


@dataclass(frozen=True)
class MediaMappingSnapshot:
    record: MediaInputMapping
    etag: str


class MediaMappingPort(Protocol):
    """A2-facing storage port; image references remain opaque to callers."""

    async def create_mapping(
        self,
        mapping: MediaInputMapping,
        *,
        orphan_ttl_seconds: int,
    ) -> MediaMappingSnapshot: ...

    async def find_by_image_ref(
        self,
        *,
        household_id: str,
        image_ref: str,
    ) -> MediaMappingSnapshot | None: ...

    async def find_completed_by_image_ref(
        self,
        *,
        household_id: str,
        image_ref: str,
    ) -> MediaMappingSnapshot | None: ...

    async def transition(
        self,
        *,
        household_id: str,
        input_id: str,
        expected_etag: str,
        status: MediaProcessingStatus,
        updated_at: datetime,
        file_size_bytes: int | None = None,
        sha256_hash: str | None = None,
        artifact_ref: str | None = None,
        failure_reason: MediaFailureReason | None = None,
    ) -> MediaMappingSnapshot: ...


class CosmosMediaMappingStorage:
    """Offline-testable media mapping storage over B1's Cosmos repositories."""

    def __init__(
        self,
        *,
        read_repository: CosmosReadRepository,
        write_client: TransactionalBatchClient,
    ) -> None:
        self._read_repository = read_repository
        self._write_client = write_client

    async def create_mapping(
        self,
        mapping: MediaInputMapping,
        *,
        orphan_ttl_seconds: int,
    ) -> MediaMappingSnapshot:
        if mapping.status is not MediaProcessingStatus.ALLOCATED:
            raise ValueError("A new media mapping must start in allocated state.")
        if (
            isinstance(orphan_ttl_seconds, bool)
            or not isinstance(orphan_ttl_seconds, int)
            or orphan_ttl_seconds <= 0
        ):
            raise ValueError("orphan_ttl_seconds must be a positive integer.")

        record = _copy_mapping(mapping, ttl_seconds=orphan_ttl_seconds)
        document = CosmosDocumentMapper.to_document(record)
        try:
            await self._write_client.execute_batch(
                partition_key=record.household_id,
                operations=(CreateDocument(document),),
            )
        except BatchOperationConflict as conflict:
            if conflict.operation_index != 0:
                raise
            existing = await self._read_repository.read_media_input_mapping_with_etag(
                household_id=record.household_id,
                input_id=record.input_id,
            )
            if existing is None:
                raise
            if CosmosDocumentMapper.to_document(existing.record) != document:
                raise MediaMappingConflict(
                    f"input_id {record.input_id!r} already maps to different values."
                ) from conflict
            return self._snapshot(existing)

        created = await self._read_repository.read_media_input_mapping_with_etag(
            household_id=record.household_id,
            input_id=record.input_id,
        )
        if created is None:
            raise RuntimeError("Created media mapping could not be read back.")
        return self._snapshot(created)

    async def find_by_image_ref(
        self,
        *,
        household_id: str,
        image_ref: str,
    ) -> MediaMappingSnapshot | None:
        found = await self._read_repository.find_media_input_mapping_by_image_ref(
            household_id=household_id,
            image_ref=image_ref,
        )
        return None if found is None else self._snapshot(found)

    async def find_completed_by_image_ref(
        self,
        *,
        household_id: str,
        image_ref: str,
    ) -> MediaMappingSnapshot | None:
        mapping = await self.find_by_image_ref(
            household_id=household_id,
            image_ref=image_ref,
        )
        if mapping is not None and mapping.record.status is not MediaProcessingStatus.COMPLETED:
            raise MediaMappingNotReady("Only completed media mappings may be used by the Agent.")
        return mapping

    async def transition(
        self,
        *,
        household_id: str,
        input_id: str,
        expected_etag: str,
        status: MediaProcessingStatus,
        updated_at: datetime,
        file_size_bytes: int | None = None,
        sha256_hash: str | None = None,
        artifact_ref: str | None = None,
        failure_reason: MediaFailureReason | None = None,
    ) -> MediaMappingSnapshot:
        if not expected_etag:
            raise ValueError("expected_etag must not be empty.")
        current = await self._read_repository.read_media_input_mapping_with_etag(
            household_id=household_id,
            input_id=input_id,
        )
        if current is None:
            raise MediaMappingNotFound(f"Media mapping {input_id!r} does not exist.")
        snapshot = self._snapshot(current)
        updated = _transitioned_mapping(
            snapshot.record,
            status=status,
            updated_at=updated_at,
            file_size_bytes=file_size_bytes,
            sha256_hash=sha256_hash,
            artifact_ref=artifact_ref,
            failure_reason=failure_reason,
        )
        if snapshot.record.status is status:
            if snapshot.record == updated:
                return snapshot
            raise MediaMappingConflict(
                f"Media mapping {input_id!r} already has a different {status.value} state."
            )
        if snapshot.etag != expected_etag:
            raise BatchOperationConflict(0)

        await self._write_client.execute_batch(
            partition_key=household_id,
            operations=(
                ReplaceDocument(
                    CosmosDocumentMapper.to_document(updated),
                    if_match_etag=expected_etag,
                ),
            ),
        )
        saved = await self._read_repository.read_media_input_mapping_with_etag(
            household_id=household_id,
            input_id=input_id,
        )
        if saved is None:
            raise RuntimeError("Updated media mapping could not be read back.")
        return self._snapshot(saved)

    @staticmethod
    def _snapshot(
        mapped: MappedDocument[MediaInputMapping],
    ) -> MediaMappingSnapshot:
        if mapped.etag is None:
            raise ValueError("Media mapping is missing its Cosmos ETag.")
        return MediaMappingSnapshot(record=mapped.record, etag=mapped.etag)


def build_media_mapping_retention_update(
    mapping: MediaMappingSnapshot,
    *,
    ttl_seconds: int | None,
) -> ReplaceDocument:
    """Build an ETag replace for inclusion with the related same-household batch."""
    if ttl_seconds is not None and (
        isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0
    ):
        raise ValueError("ttl_seconds must be a positive integer or None.")

    updated = _copy_mapping(mapping.record, ttl_seconds=ttl_seconds)
    return ReplaceDocument(
        CosmosDocumentMapper.to_document(updated),
        if_match_etag=mapping.etag,
    )


def _copy_mapping(
    mapping: MediaInputMapping,
    *,
    ttl_seconds: int | None,
    **updates: object,
) -> MediaInputMapping:
    values: dict[str, object] = mapping.model_dump(mode="python")
    values["ttl_seconds"] = ttl_seconds
    values.update(updates)
    return MediaInputMapping.model_validate(values)


def _transitioned_mapping(
    current: MediaInputMapping,
    *,
    status: MediaProcessingStatus,
    updated_at: datetime,
    file_size_bytes: int | None,
    sha256_hash: str | None,
    artifact_ref: str | None,
    failure_reason: MediaFailureReason | None,
) -> MediaInputMapping:
    if (
        status not in _ALLOWED_TRANSITIONS.get(current.status, set())
        and status is not current.status
    ):
        raise MediaMappingConflict(
            f"Cannot transition media mapping from {current.status.value} to {status.value}."
        )

    values: Mapping[str, object] = {
        "status": status,
        "updated_at": updated_at,
    }
    if status is MediaProcessingStatus.RECEIVED:
        if file_size_bytes is None or sha256_hash is None:
            raise ValueError("received state requires file_size_bytes and sha256_hash.")
        values = {**values, "file_size_bytes": file_size_bytes, "sha256_hash": sha256_hash}
    elif status is MediaProcessingStatus.COMPLETED:
        if not artifact_ref:
            raise ValueError("completed state requires artifact_ref.")
        values = {**values, "artifact_ref": artifact_ref}
    elif status is MediaProcessingStatus.FAILED:
        if failure_reason is None:
            raise ValueError("failed state requires failure_reason.")
        values = {**values, "failure_reason": failure_reason}
    elif any(
        value is not None for value in (file_size_bytes, sha256_hash, artifact_ref, failure_reason)
    ):
        raise ValueError(f"{status.value} transition does not accept completion or failure data.")

    return _copy_mapping(current, ttl_seconds=current.ttl_seconds, **values)

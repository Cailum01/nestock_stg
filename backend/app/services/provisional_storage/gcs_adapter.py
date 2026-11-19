"""Private Google Cloud Storage adapter for household-scoped media."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from app.models.domain import MediaProcessingStatus
from app.services.media_mapping import MediaMappingNotReady, MediaMappingPort
from app.services.provisional_storage._lifecycle import BaseMediaStorageLifecycle

logger = logging.getLogger(__name__)


class GCSMediaStorage(BaseMediaStorageLifecycle):
    """Server-mediated upload adapter; it never creates public URLs or ACLs."""

    def __init__(
        self,
        *,
        bucket: Any,
        base_staging_url: str | None = None,
        mapping_port: MediaMappingPort | None = None,
        auto_process: bool = True,
        vision_service: Any | None = None,
    ) -> None:
        super().__init__(
            base_staging_url=base_staging_url,
            mapping_port=mapping_port,
            auto_process=auto_process,
            vision_service=vision_service,
        )
        self._bucket = bucket

    @staticmethod
    def _parse_media_ref(household_id: str, image_ref: str) -> tuple[str, str]:
        prefix = f"blob://households/{household_id}/media/"
        if not household_id or not image_ref.startswith(prefix):
            raise ValueError("Media reference does not belong to the authorized household.")
        name = image_ref[len(prefix) :]
        if not name or "/" in name or name in {".", ".."}:
            raise ValueError("Media reference is invalid.")
        return name, name.rsplit(".", 1)[0]

    @staticmethod
    def _parse_artifact_ref(household_id: str, artifact_ref: str) -> str:
        prefix = f"blob://households/{household_id}/media/"
        if not household_id or not artifact_ref.startswith(prefix):
            raise ValueError("Artifact reference does not belong to the authorized household.")
        parts = artifact_ref[len(prefix) :].split("/")
        if len(parts) != 3 or not parts[0] or parts[1] != "derived" or parts[2] != "vision_v1.json":
            raise ValueError("Artifact reference is invalid.")
        return parts[0]

    @staticmethod
    def _raw_name(household_id: str, filename: str, retention: str) -> str:
        folder = "attached" if retention == "attached" else "staging"
        return f"households/{household_id}/{folder}/{filename}"

    @staticmethod
    def _artifact_name(household_id: str, input_id: str, retention: str) -> str:
        folder = "attached-artifacts" if retention == "attached" else "staging-artifacts"
        return f"households/{household_id}/{folder}/{input_id}/vision_v1.json"

    def _blob(self, name: str) -> Any:
        return self._bucket.blob(name)

    @staticmethod
    def _not_found(error: Exception) -> bool:
        return getattr(error, "code", None) == 404 or error.__class__.__name__ in {
            "NotFound",
            "NoSuchKey",
        }

    async def _download_owned(self, name: str, household_id: str) -> bytes | None:
        blob = self._blob(name)

        def download() -> bytes | None:
            try:
                blob.reload()
            except Exception as error:
                if self._not_found(error):
                    return None
                raise
            metadata = blob.metadata or {}
            if metadata.get("household_id") != household_id:
                return None
            return bytes(blob.download_as_bytes())

        return await asyncio.to_thread(download)

    async def _store_raw_media(
        self,
        household_id: str,
        image_ref: str,
        payload: bytes,
        content_type: str,
        record: dict[str, Any],
    ) -> None:
        filename, input_id = self._parse_media_ref(household_id, image_ref)
        name = self._raw_name(household_id, filename, "orphan")
        metadata = self._blob_metadata(household_id, input_id, record)
        blob = self._blob(name)

        def upload() -> None:
            blob.metadata = metadata
            blob.upload_from_string(
                payload,
                content_type=content_type,
                if_generation_match=0,
            )

        await asyncio.to_thread(upload)

    async def _store_artifact_bytes(
        self,
        household_id: str,
        image_ref: str,
        artifact_ref: str,
        payload: bytes,
        record: dict[str, Any],
    ) -> None:
        _, input_id = self._parse_media_ref(household_id, image_ref)
        if self._parse_artifact_ref(household_id, artifact_ref) != input_id:
            raise ValueError("Derived artifact does not belong to the authorized media record.")
        name = self._artifact_name(household_id, input_id, str(record.get("retention", "orphan")))
        blob = self._blob(name)

        def upload() -> None:
            blob.metadata = self._blob_metadata(household_id, input_id, record)
            blob.upload_from_string(
                payload,
                content_type="application/json",
                if_generation_match=0,
            )

        await asyncio.to_thread(upload)

    def _blob_metadata(
        self, household_id: str, input_id: str, record: dict[str, Any]
    ) -> dict[str, str]:
        expiration = record.get("expires_at")
        return {
            "household_id": household_id,
            "input_id": input_id,
            "retention": str(record.get("retention", "orphan")),
            "expires_at": expiration.isoformat() if isinstance(expiration, datetime) else "",
        }

    async def _mark_storage_retained(
        self, household_id: str, image_ref: str, record: dict[str, Any]
    ) -> None:
        filename, input_id = self._parse_media_ref(household_id, image_ref)
        metadata = self._blob_metadata(household_id, input_id, record)
        names = (
            self._raw_name(household_id, filename, "orphan"),
            self._raw_name(household_id, filename, "attached"),
            self._artifact_name(household_id, input_id, "orphan"),
            self._artifact_name(household_id, input_id, "attached"),
        )
        for name in names:
            await asyncio.to_thread(self._update_owned_metadata, name, household_id, metadata)

    def _update_owned_metadata(
        self, name: str, household_id: str, metadata: dict[str, str]
    ) -> None:
        blob = self._blob(name)
        try:
            blob.reload()
        except Exception as error:
            if self._not_found(error):
                return
            raise
        existing = dict(blob.metadata or {})
        if existing.get("household_id") != household_id:
            raise ValueError("Stored media ownership metadata does not match the request.")
        blob.metadata = metadata
        blob.patch()

    async def _delete_if_owned(self, name: str, household_id: str) -> None:
        blob = self._blob(name)

        def delete() -> None:
            try:
                blob.reload()
            except Exception as error:
                if self._not_found(error):
                    return
                raise
            if (blob.metadata or {}).get("household_id") != household_id:
                raise ValueError("Refusing to delete media owned by a different household.")
            blob.delete(if_generation_match=blob.generation)

        await asyncio.to_thread(delete)

    async def _delete_media_and_artifact(
        self, household_id: str, image_ref: str, record: dict[str, Any]
    ) -> None:
        del record
        filename, input_id = self._parse_media_ref(household_id, image_ref)
        names = (
            self._raw_name(household_id, filename, "orphan"),
            self._raw_name(household_id, filename, "attached"),
            self._artifact_name(household_id, input_id, "orphan"),
            self._artifact_name(household_id, input_id, "attached"),
        )
        for name in names:
            await self._delete_if_owned(name, household_id)

    async def _copy_owned(self, source_name: str, destination_name: str, household_id: str) -> bool:
        source = self._blob(source_name)

        def copy() -> bool:
            try:
                source.reload()
            except Exception as error:
                if self._not_found(error):
                    return False
                raise
            metadata = dict(source.metadata or {})
            if metadata.get("household_id") != household_id:
                raise ValueError("Stored media ownership metadata does not match the request.")
            if self._blob(destination_name).exists():
                return True
            self._bucket.copy_blob(
                source,
                self._bucket,
                destination_name,
                if_generation_match=0,
            )
            source.delete(if_generation_match=source.generation)
            return True

        return await asyncio.to_thread(copy)

    async def retain_media(self, household_id: str, image_ref: str) -> bool:
        snapshot = await self.get_media_snapshot(household_id, image_ref)
        if snapshot is None or snapshot.record.status is not MediaProcessingStatus.COMPLETED:
            return False
        if not await self.verify_media_exists(household_id, image_ref):
            return False
        return await super().retain_media(household_id, image_ref)

    async def verify_media_exists(self, household_id: str, image_ref: str) -> bool:
        try:
            filename, _ = self._parse_media_ref(household_id, image_ref)
            snapshot = (
                await self._mapping_port.find_completed_by_image_ref(
                    household_id=household_id,
                    image_ref=image_ref,
                )
                if self._mapping_port is not None
                else None
            )
        except MediaMappingNotReady:
            return False
        if snapshot is None:
            return False
        for retention in ("attached", "orphan"):
            name = self._raw_name(household_id, filename, retention)
            if await self._download_owned(name, household_id) is not None:
                return True
        return False

    async def get_media_bytes(self, household_id: str, image_ref: str) -> bytes | None:
        filename, _ = self._parse_media_ref(household_id, image_ref)
        for retention in ("attached", "orphan"):
            data = await self._download_owned(
                self._raw_name(household_id, filename, retention),
                household_id,
            )
            if data is not None:
                return data
        return None

    async def get_derived_artifact(self, household_id: str, image_ref: str) -> bytes | None:
        snapshot = await self.get_media_snapshot(household_id, image_ref)
        if snapshot is None or snapshot.record.artifact_ref is None:
            return None
        input_id = self._parse_artifact_ref(household_id, snapshot.record.artifact_ref)
        for retention in ("attached", "orphan"):
            data = await self._download_owned(
                self._artifact_name(household_id, input_id, retention),
                household_id,
            )
            if data is not None:
                return data
        return None

    async def cleanup_expired_blobs(self, now: datetime | None = None) -> list[str]:
        """Purge expired orphan objects; no scheduler is installed by this adapter."""
        effective_now = now or datetime.now(UTC)
        blobs = await asyncio.to_thread(lambda: list(self._bucket.list_blobs(prefix="households/")))
        removed: set[str] = set()
        for blob in blobs:
            metadata = blob.metadata or {}
            if metadata.get("retention") != "orphan":
                continue
            expiration = metadata.get("expires_at")
            try:
                expires_at = datetime.fromisoformat(str(expiration).replace("Z", "+00:00"))
            except ValueError:
                continue
            if expires_at.tzinfo is None or effective_now < expires_at:
                continue
            household_id = metadata.get("household_id")
            if not household_id or not blob.name.startswith(f"households/{household_id}/"):
                continue
            await self._delete_if_owned(blob.name, household_id)
            remainder = blob.name.removeprefix(f"households/{household_id}/")
            segments = remainder.split("/")
            if len(segments) == 2 and segments[0] in {"staging", "attached"}:
                removed.add(f"blob://households/{household_id}/media/{segments[1]}")
            elif metadata.get("input_id"):
                removed.add(f"blob://households/{household_id}/media/{metadata['input_id']}")
        return sorted(removed)

    async def _resolve_blob(self, household_id: str, image_ref: str) -> str | None:
        filename, _ = self._parse_media_ref(household_id, image_ref)
        for retention in ("attached", "orphan"):
            name = self._raw_name(household_id, filename, retention)
            if await self._download_owned(name, household_id) is not None:
                return name
        return None

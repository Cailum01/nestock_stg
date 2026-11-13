"""[PROVISIONAL_STAGING] Azure Blob 影像儲存適配器 (Azure Blob Media Storage).

================================================================================
【架構實作宣告 / AZURE BLOB STORAGE ADAPTER】
- 實作 MediaStorageService 協定，負責二進位圖檔與衍生分析工件於 Azure Blob 之存取。
- 繼承 BaseMediaStorageLifecycle，嚴格維持與 B1 MediaMappingPort 之五態流轉、
  衍生工件先落盤後 COMPLETED、Fail-Closed 門禁與孤立 Blob 到期清理。
- 同時相容 Azure Blob 同步與非同步 (aio) 客戶端，避免阻斷非同步事件迴圈。
================================================================================
"""

import inspect
import logging
from datetime import datetime
from typing import Any

from app.services.media_mapping import MediaMappingNotReady, MediaMappingPort
from app.services.provisional_storage._lifecycle import BaseMediaStorageLifecycle

logger = logging.getLogger(__name__)


class AzureBlobMediaStorage(BaseMediaStorageLifecycle):
    """Production storage adapter connecting to Azure Blob Storage with Cosmos MediaMappingPort."""

    def __init__(
        self,
        blob_service_client: Any,
        container_name: str = "media",
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
        self._blob_service_client = blob_service_client
        self._container_name = container_name

    def _ref_to_blob_path(self, image_ref: str) -> str:
        """Convert opaque image_ref (blob://...) to container relative blob path."""
        return image_ref.removeprefix("blob://").lstrip("/")

    async def _resolve_client(self, blob_path: str) -> Any:
        container_client = self._blob_service_client.get_container_client(self._container_name)
        if inspect.isawaitable(container_client):
            container_client = await container_client
        blob_client = container_client.get_blob_client(blob_path)
        return await blob_client if inspect.isawaitable(blob_client) else blob_client

    async def _call_client(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        if inspect.iscoroutinefunction(func):
            return await func(*args, **kwargs)
        res = func(*args, **kwargs)
        return await res if inspect.isawaitable(res) else res

    def _blob_meta(self, household_id: str, rec: dict[str, Any]) -> dict[str, str]:
        expires_at = rec.get("expires_at")
        return {
            "household_id": household_id,
            "input_id": str(rec.get("input_id", "")),
            "retention": str(rec.get("retention", "orphan")),
            "expires_at": expires_at.isoformat() if isinstance(expires_at, datetime) else "",
        }

    async def _upload_to_blob(
        self,
        blob_path: str,
        data: bytes | str,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> None:
        blob_client = await self._resolve_client(blob_path)
        kwargs: dict[str, Any] = {"overwrite": True}
        if content_type:
            kwargs["content_type"] = content_type
        if metadata is not None:
            kwargs["metadata"] = metadata
        await self._call_client(blob_client.upload_blob, data, **kwargs)

    async def _download_from_blob(self, blob_path: str) -> bytes | None:
        try:
            blob_client = await self._resolve_client(blob_path)
            stream = await self._call_client(blob_client.download_blob)
            for method_name in ("readall", "read"):
                reader = getattr(stream, method_name, None)
                if reader is not None:
                    return await self._call_client(reader)  # type: ignore[no-any-return]
            return bytes(stream)
        except Exception as exc:
            logger.debug("Blob download failed for path %s: %s", blob_path, exc)
            return None

    async def _blob_exists(self, blob_path: str) -> bool:
        try:
            blob_client = await self._resolve_client(blob_path)
            exists_func = getattr(blob_client, "exists", None)
            return bool(await self._call_client(exists_func)) if exists_func is not None else True
        except Exception:
            return False

    async def _delete_blob(self, blob_path: str) -> None:
        try:
            blob_client = await self._resolve_client(blob_path)
            delete_func = getattr(blob_client, "delete_blob", None)
            if delete_func is not None:
                await self._call_client(delete_func)
        except Exception as exc:
            cls_name = exc.__class__.__name__
            status_code = getattr(exc, "status_code", None)
            error_code = getattr(exc, "error_code", None)
            if (
                cls_name == "ResourceNotFoundError"
                or status_code == 404
                or error_code in {"BlobNotFound", "ResourceNotFound"}
            ):
                logger.debug("Blob %s already deleted or not found: %s", blob_path, exc)
                return
            logger.error("Failed to delete blob at %s: %s", blob_path, exc)
            raise

    async def _set_blob_metadata(self, blob_path: str, metadata: dict[str, str]) -> None:
        try:
            blob_client = await self._resolve_client(blob_path)
            meta_func = getattr(blob_client, "set_blob_metadata", None)
            if meta_func is not None:
                await self._call_client(meta_func, metadata=metadata)
        except Exception as exc:
            logger.debug("Blob metadata update skipped for %s: %s", blob_path, exc)

    async def _store_raw_media(
        self, hid: str, image_ref: str, payload: bytes, content_type: str, rec: dict[str, Any]
    ) -> None:
        await self._upload_to_blob(
            self._ref_to_blob_path(image_ref),
            payload,
            content_type=content_type,
            metadata=self._blob_meta(hid, rec),
        )

    async def _store_artifact_bytes(
        self, hid: str, image_ref: str, artifact_ref: str, payload: bytes, rec: dict[str, Any]
    ) -> None:
        del image_ref
        await self._upload_to_blob(
            self._ref_to_blob_path(artifact_ref),
            payload,
            content_type="application/json",
            metadata=self._blob_meta(hid, rec),
        )

    async def _mark_storage_retained(self, hid: str, image_ref: str, rec: dict[str, Any]) -> None:
        meta = self._blob_meta(hid, rec)
        await self._set_blob_metadata(self._ref_to_blob_path(image_ref), meta)
        if rec.get("artifact_ref"):
            await self._set_blob_metadata(self._ref_to_blob_path(str(rec["artifact_ref"])), meta)

    async def _delete_media_and_artifact(
        self, hid: str, image_ref: str, rec: dict[str, Any]
    ) -> None:
        await self._delete_blob(self._ref_to_blob_path(image_ref))
        artifact_ref = rec.get("artifact_ref") or (
            f"blob://households/{hid}/media/{rec['input_id']}/derived/vision_v1.json"
            if rec.get("input_id")
            else None
        )
        if artifact_ref:
            await self._delete_blob(self._ref_to_blob_path(str(artifact_ref)))

    async def verify_media_exists(self, household_id: str, image_ref: str) -> bool:
        """Check whether media exists, belongs to household, and is COMPLETED (Fail-Closed)."""
        if not image_ref.startswith(f"blob://households/{household_id}/"):
            return False
        if self._mapping_port is None:
            return False
        rec = self._retention_records.get((household_id, image_ref))
        if rec is not None and rec.get("purged"):
            return False
        try:
            snapshot = await self._mapping_port.find_completed_by_image_ref(
                household_id=household_id, image_ref=image_ref
            )
        except MediaMappingNotReady:
            return False
        except Exception as exc:
            logger.error(
                "Unexpected persistence error verifying media existence for %s %s: %s",
                household_id,
                image_ref,
                exc,
                exc_info=True,
            )
            raise
        if snapshot is None or not await self._blob_exists(self._ref_to_blob_path(image_ref)):
            return False
        if snapshot.record.artifact_ref:
            return await self._blob_exists(self._ref_to_blob_path(snapshot.record.artifact_ref))
        return True

    async def get_media_bytes(self, household_id: str, image_ref: str) -> bytes | None:
        """Retrieve media raw binary bytes from Azure Blob for the specified household."""
        if not image_ref.startswith(f"blob://households/{household_id}/"):
            return None
        rec = self._retention_records.get((household_id, image_ref))
        if rec is not None and rec.get("purged"):
            return None
        return await self._download_from_blob(self._ref_to_blob_path(image_ref))

    async def get_derived_artifact(self, household_id: str, image_ref: str) -> bytes | None:
        """Retrieve persisted derived vision analysis artifact bytes from Azure Blob."""
        if not image_ref.startswith(f"blob://households/{household_id}/"):
            return None
        rec = self._retention_records.get((household_id, image_ref))
        if rec is not None and rec.get("purged"):
            return None
        artifact_ref = rec.get("artifact_ref") if rec else None
        if not artifact_ref:
            snap = self._snapshots.get((household_id, image_ref)) or await self.get_media_snapshot(
                household_id, image_ref
            )
            artifact_ref = snap.record.artifact_ref if snap else None
        if not artifact_ref:
            return None
        return await self._download_from_blob(self._ref_to_blob_path(str(artifact_ref)))

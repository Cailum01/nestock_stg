"""[PROVISIONAL_STAGING] 本機影像儲存適配器 (Local Staging Media Storage).

================================================================================
【過渡期模組宣告 / PROVISIONAL STAGING】
- 支援直接操作記憶體暫存 (Local Memory Staging) 以及掛載 B1 MediaMappingPort
  (CosmosMediaMappingStorage)。
- 繼承 BaseMediaStorageLifecycle，嚴格落實五態流轉 (ALLOCATED -> RECEIVED -> PROCESSING
  -> COMPLETED / FAILED)、SHA-256 校驗、衍生工件持久化與 ETag 樂觀鎖。
- Agent 門禁防禦：verify_media_exists 內部調用 find_completed_by_image_ref，
  非 COMPLETED 狀態或跨戶查閱一律阻斷。
================================================================================
"""

import logging
from typing import Any

from app.models.domain import MediaProcessingStatus
from app.services.media_mapping import MediaMappingNotReady, MediaMappingPort
from app.services.provisional_storage._lifecycle import BaseMediaStorageLifecycle

logger = logging.getLogger(__name__)


class LocalStagingMediaStorage(BaseMediaStorageLifecycle):
    """Staging adapter supporting in-memory buffers and optional Cosmos MediaMappingPort."""

    def __init__(
        self,
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
        self._staged_media: dict[tuple[str, str], bytes] = {}
        self._staged_artifacts: dict[tuple[str, str], bytes] = {}

    async def _store_raw_media(
        self, hid: str, image_ref: str, payload: bytes, content_type: str, rec: dict[str, Any]
    ) -> None:
        del content_type, rec
        self._staged_media[(hid, image_ref)] = payload

    async def _store_artifact_bytes(
        self, hid: str, image_ref: str, artifact_ref: str, payload: bytes, rec: dict[str, Any]
    ) -> None:
        del artifact_ref, rec
        self._staged_artifacts[(hid, image_ref)] = payload

    async def _delete_media_and_artifact(
        self, hid: str, image_ref: str, rec: dict[str, Any]
    ) -> None:
        del rec
        self._staged_media.pop((hid, image_ref), None)
        self._staged_artifacts.pop((hid, image_ref), None)

    async def verify_media_exists(self, household_id: str, image_ref: str) -> bool:
        """Check whether media exists, belongs to household, and is ready for agent."""
        rec = self._retention_records.get((household_id, image_ref))
        if rec is not None and rec.get("purged"):
            return False
        if self._mapping_port is not None:
            try:
                snapshot = await self._mapping_port.find_completed_by_image_ref(
                    household_id=household_id,
                    image_ref=image_ref,
                )
                return snapshot is not None
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

        if (household_id, image_ref) not in self._staged_media:
            return False
        return self._local_status.get((household_id, image_ref)) == MediaProcessingStatus.COMPLETED

    async def get_media_bytes(self, household_id: str, image_ref: str) -> bytes | None:
        """Retrieve media raw binary bytes if present for the specified household."""
        rec = self._retention_records.get((household_id, image_ref))
        if rec is not None and rec.get("purged"):
            return None
        return self._staged_media.get((household_id, image_ref))

    async def get_derived_artifact(self, household_id: str, image_ref: str) -> bytes | None:
        """Retrieve persisted derived vision analysis artifact bytes from local staging."""
        rec = self._retention_records.get((household_id, image_ref))
        if rec is not None and rec.get("purged"):
            return None
        return self._staged_artifacts.get((household_id, image_ref))

    async def simulate_client_upload(
        self, household_id: str, image_ref: str, payload_bytes: bytes
    ) -> None:
        """Helper method for local tests simulating client direct upload to storage."""
        self._staged_media[(household_id, image_ref)] = payload_bytes
        self._local_status[(household_id, image_ref)] = MediaProcessingStatus.COMPLETED

"""[PROVISIONAL_STAGING] 本機影像儲存適配器 (Local Staging Media Storage).

================================================================================
【過渡期模組宣告 / PROVISIONAL STAGING】
- 支援直接操作記憶體暫存 (Local Memory Staging) 以及掛載 B1 MediaMappingPort
  (CosmosMediaMappingStorage)。
- 掛載 MediaMappingPort 時，嚴格落實五態流轉 (ALLOCATED -> RECEIVED -> PROCESSING
  -> COMPLETED / FAILED)、SHA-256 校驗、衍生工件 artifact_ref 生成與 ETag 樂觀鎖。
- Agent 門禁防禦：verify_media_exists 內部調用 find_completed_by_image_ref，
  非 COMPLETED 狀態或跨戶查閱一律阻斷。
================================================================================
"""

import hashlib
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.models.domain import (
    MediaFailureReason,
    MediaInputMapping,
    MediaProcessingStatus,
)
from app.models.storage import (
    AllowedMediaContentType,
    UploadAuthorizationRequest,
    UploadAuthorizationResponse,
)
from app.services.media_mapping import (
    MediaMappingNotReady,
    MediaMappingPort,
    MediaMappingSnapshot,
)

logger = logging.getLogger(__name__)


class LocalStagingMediaStorage:
    """Staging adapter supporting in-memory buffers and optional Cosmos MediaMappingPort."""

    def __init__(
        self,
        base_staging_url: str | None = None,
        mapping_port: MediaMappingPort | None = None,
        auto_process: bool = True,
    ) -> None:
        self._configured_base_url = base_staging_url
        self._mapping_port = mapping_port
        self._auto_process = auto_process
        # In-memory registry tracking staged items: (household_id, image_ref) -> bytes
        self._staged_media: dict[tuple[str, str], bytes] = {}
        # In-memory registry tracking pending authorization tokens
        self._pending_tokens: dict[str, dict] = {}
        # In-memory snapshot cache for fast lookup during active turn:
        # (household_id, image_ref) -> snapshot
        self._snapshots: dict[tuple[str, str], MediaMappingSnapshot] = {}

    @property
    def _base_staging_url(self) -> str:
        return (self._configured_base_url or "http://127.0.0.1:8000/staging/upload").rstrip("/")

    def _determine_extension(self, filename: str, content_type: AllowedMediaContentType) -> str:
        suffix = Path(filename).suffix.lower()
        if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
            return suffix
        if content_type == AllowedMediaContentType.IMAGE_PNG:
            return ".png"
        if content_type == AllowedMediaContentType.IMAGE_WEBP:
            return ".webp"
        return ".jpg"

    async def create_upload_authorization(
        self,
        request: UploadAuthorizationRequest,
        *,
        base_staging_url: str | None = None,
    ) -> UploadAuthorizationResponse:
        """Simulate issuing a pre-signed SAS upload authorization and register ALLOCATED mapping."""
        media_id = uuid.uuid4().hex
        ext = self._determine_extension(request.filename, request.content_type)
        image_ref = f"blob://households/{request.household_id}/media/{media_id}{ext}"

        effective_base = (
            self._configured_base_url or base_staging_url or "http://127.0.0.1:8000/staging/upload"
        ).rstrip("/")
        upload_token = f"staging-token-{media_id}"
        upload_url = f"{effective_base}/{upload_token}"

        now = datetime.now(UTC)
        expires_at = time.time() + 300

        self._pending_tokens[upload_token] = {
            "input_id": media_id,
            "household_id": request.household_id,
            "image_ref": image_ref,
            "content_type": str(request.content_type),
            "expires_at": expires_at,
            "file_size_bytes": request.file_size_bytes,
        }

        if self._mapping_port is not None:
            mapping = MediaInputMapping(
                input_id=media_id,
                household_id=request.household_id,
                image_ref=image_ref,
                content_type=str(request.content_type),
                expected_file_size_bytes=request.file_size_bytes,
                status=MediaProcessingStatus.ALLOCATED,
                upload_authorization_expires_at=now + timedelta(seconds=300),
                created_at=now,
                updated_at=now,
            )
            snapshot = await self._mapping_port.create_mapping(mapping, orphan_ttl_seconds=86400)
            self._snapshots[(request.household_id, image_ref)] = snapshot

        return UploadAuthorizationResponse(
            upload_url=upload_url,
            image_ref=image_ref,
            expires_in_seconds=300,
            required_headers={
                "Content-Type": str(request.content_type),
                "x-ms-blob-type": "BlockBlob",
            },
        )

    async def receive_staged_upload(
        self, upload_token: str, payload_bytes: bytes
    ) -> tuple[str, str]:
        """Receive raw binary upload payload, validate token, and persist to staging."""
        token_info = self._pending_tokens.get(upload_token)
        if not token_info:
            raise KeyError(f"Upload token '{upload_token}' not found or already consumed")

        household_id = token_info["household_id"]
        image_ref = token_info["image_ref"]
        input_id = token_info["input_id"]

        if time.time() > token_info["expires_at"]:
            self._pending_tokens.pop(upload_token, None)
            if self._mapping_port is not None:
                try:
                    await self.fail_processing(
                        household_id, image_ref, MediaFailureReason.UPLOAD_TIMEOUT
                    )
                except Exception as exc:
                    logger.error(
                        "Failed to record FAILED state on upload timeout for image_ref=%s: %s",
                        image_ref,
                        exc,
                        exc_info=True,
                    )
                    raise
            raise TimeoutError(f"Upload token '{upload_token}' has expired")

        if len(payload_bytes) == 0:
            if self._mapping_port is not None:
                try:
                    await self.fail_processing(
                        household_id, image_ref, MediaFailureReason.VALIDATION_FAILED
                    )
                except Exception as exc:
                    logger.error(
                        "Failed to record FAILED state on empty payload for image_ref=%s: %s",
                        image_ref,
                        exc,
                        exc_info=True,
                    )
                    raise
            raise ValueError("Payload cannot be empty")

        if len(payload_bytes) > 10_485_760:
            if self._mapping_port is not None:
                try:
                    await self.fail_processing(
                        household_id, image_ref, MediaFailureReason.VALIDATION_FAILED
                    )
                except Exception as exc:
                    logger.error(
                        "Failed to record FAILED state on oversized payload for image_ref=%s: %s",
                        image_ref,
                        exc,
                        exc_info=True,
                    )
                    raise
            raise ValueError(
                f"Payload exceeds maximum allowed size of 10MB: {len(payload_bytes)} bytes"
            )

        self._staged_media[(household_id, image_ref)] = payload_bytes
        self._pending_tokens.pop(upload_token, None)

        if self._mapping_port is not None:
            sha256_hash = hashlib.sha256(payload_bytes).hexdigest()
            file_size_bytes = len(payload_bytes)
            snap = await self._get_snapshot(household_id, image_ref)
            now = datetime.now(UTC)
            updated_snap = await self._mapping_port.transition(
                household_id=household_id,
                input_id=input_id,
                expected_etag=snap.etag,
                status=MediaProcessingStatus.RECEIVED,
                updated_at=now,
                file_size_bytes=file_size_bytes,
                sha256_hash=sha256_hash,
            )
            self._snapshots[(household_id, image_ref)] = updated_snap

            if self._auto_process:
                await self.begin_processing(household_id, image_ref)
                await self.complete_processing(household_id, image_ref)

        return (household_id, image_ref)

    async def begin_processing(self, household_id: str, image_ref: str) -> None:
        """Advance media mapping state to PROCESSING."""
        if self._mapping_port is not None:
            snap = await self._get_snapshot(household_id, image_ref)
            if snap.record.status == MediaProcessingStatus.COMPLETED:
                return
            now = datetime.now(UTC)
            updated_snap = await self._mapping_port.transition(
                household_id=household_id,
                input_id=snap.record.input_id,
                expected_etag=snap.etag,
                status=MediaProcessingStatus.PROCESSING,
                updated_at=now,
            )
            self._snapshots[(household_id, image_ref)] = updated_snap

    async def complete_processing(
        self,
        household_id: str,
        image_ref: str,
        artifact_data: str | bytes | None = None,
    ) -> str:
        """Advance media mapping state to COMPLETED and record derived artifact reference."""
        snap = self._snapshots.get((household_id, image_ref))
        if snap is None and self._mapping_port is not None:
            snap = await self._get_snapshot(household_id, image_ref)
        input_id = snap.record.input_id if snap else "mock"
        artifact_ref = f"blob://households/{household_id}/media/{input_id}/derived/vision_v1.json"

        if self._mapping_port is not None and snap is not None:
            if snap.record.status == MediaProcessingStatus.COMPLETED:
                return snap.record.artifact_ref or artifact_ref
            now = datetime.now(UTC)
            updated_snap = await self._mapping_port.transition(
                household_id=household_id,
                input_id=snap.record.input_id,
                expected_etag=snap.etag,
                status=MediaProcessingStatus.COMPLETED,
                updated_at=now,
                artifact_ref=artifact_ref,
            )
            self._snapshots[(household_id, image_ref)] = updated_snap

        return artifact_ref

    async def fail_processing(
        self,
        household_id: str,
        image_ref: str,
        failure_reason: MediaFailureReason = MediaFailureReason.PROCESSING_FAILED,
    ) -> None:
        """Advance media mapping state to FAILED with explicit failure reason."""
        if self._mapping_port is not None:
            snap = await self._get_snapshot(household_id, image_ref)
            now = datetime.now(UTC)
            updated_snap = await self._mapping_port.transition(
                household_id=household_id,
                input_id=snap.record.input_id,
                expected_etag=snap.etag,
                status=MediaProcessingStatus.FAILED,
                updated_at=now,
                failure_reason=failure_reason,
            )
            self._snapshots[(household_id, image_ref)] = updated_snap

    async def verify_media_exists(self, household_id: str, image_ref: str) -> bool:
        """Check whether media exists, belongs to household, and is ready for agent."""
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
                    "Unexpected persistence error verifying media existence for "
                    "household_id=%s image_ref=%s: %s",
                    household_id,
                    image_ref,
                    exc,
                    exc_info=True,
                )
                raise

        return (household_id, image_ref) in self._staged_media

    async def get_media_bytes(self, household_id: str, image_ref: str) -> bytes | None:
        """Retrieve media raw binary bytes if present for the specified household."""
        return self._staged_media.get((household_id, image_ref))

    async def get_media_snapshot(
        self, household_id: str, image_ref: str
    ) -> MediaMappingSnapshot | None:
        """Retrieve the authoritative MediaMappingSnapshot for downstream atomic retention."""
        if self._mapping_port is not None:
            return await self._mapping_port.find_by_image_ref(
                household_id=household_id,
                image_ref=image_ref,
            )
        return None

    async def simulate_client_upload(
        self, household_id: str, image_ref: str, payload_bytes: bytes
    ) -> None:
        """Helper method for local tests simulating client direct upload to storage."""
        self._staged_media[(household_id, image_ref)] = payload_bytes

    async def _get_snapshot(self, household_id: str, image_ref: str) -> MediaMappingSnapshot:
        snapshot = self._snapshots.get((household_id, image_ref))
        if snapshot is None and self._mapping_port is not None:
            snapshot = await self._mapping_port.find_by_image_ref(
                household_id=household_id,
                image_ref=image_ref,
            )
        if snapshot is None:
            raise KeyError(
                f"Media mapping not found for image_ref '{image_ref}' "
                f"in household '{household_id}'."
            )
        return snapshot

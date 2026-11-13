"""[PROVISIONAL_STAGING] 媒體儲存生命週期共用基底 (Media Storage Lifecycle Base).
封裝雲端與本機儲存適配器共用之授權 Token、五態流轉、Ingest 分析與孤立清理邏輯。
"""

import hashlib
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.models.domain import MediaFailureReason, MediaInputMapping, MediaProcessingStatus
from app.models.storage import (
    AllowedMediaContentType,
    UploadAuthorizationRequest,
    UploadAuthorizationResponse,
)
from app.services.media_mapping import MediaMappingPort, MediaMappingSnapshot

logger = logging.getLogger(__name__)
ORPHAN_TTL_SECONDS = 86400
MAX_UPLOAD_BYTES = 10_485_760
DEFAULT_REASON = MediaFailureReason.PROCESSING_FAILED


class BaseMediaStorageLifecycle:
    """Shared lifecycle orchestration for media upload, state transitions, and retention."""

    def __init__(
        self,
        *,
        base_staging_url: str | None = None,
        mapping_port: MediaMappingPort | None = None,
        auto_process: bool = True,
        vision_service: Any | None = None,
    ) -> None:
        self._configured_base_url = base_staging_url
        self._mapping_port = mapping_port
        self._auto_process = auto_process
        self._vision_service = vision_service
        self._pending_tokens: dict[str, dict[str, Any]] = {}
        self._snapshots: dict[tuple[str, str], MediaMappingSnapshot] = {}
        self._retention_records: dict[tuple[str, str], dict[str, Any]] = {}

    def bind_vision_service(self, vision_service: Any) -> None:
        """Bind a VisionService instance for automatic ingest analysis."""
        self._vision_service = vision_service

    @property
    def _base_staging_url(self) -> str:
        return (self._configured_base_url or "http://127.0.0.1:8000/staging/upload").rstrip("/")

    def _determine_extension(self, filename: str, content_type: AllowedMediaContentType) -> str:
        suffix = Path(filename).suffix.lower()
        if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
            return suffix
        if content_type == AllowedMediaContentType.IMAGE_PNG:
            return ".png"
        return ".webp" if content_type == AllowedMediaContentType.IMAGE_WEBP else ".jpg"

    def _init_retention(self, hid: str, ref: str, iid: str, now: datetime) -> dict[str, Any]:
        exp = now + timedelta(seconds=ORPHAN_TTL_SECONDS)
        rec = {"input_id": iid, "retention": "orphan", "expires_at": exp, "purged": False}
        self._retention_records[(hid, ref)] = rec
        return rec

    async def _transition_mapping(
        self, hid: str, ref: str, status: MediaProcessingStatus, **fields: Any
    ) -> None:
        if self._mapping_port is None:
            return
        snap = await self._get_snapshot(hid, ref)
        kw = {"household_id": hid, "input_id": snap.record.input_id, "expected_etag": snap.etag}
        res = await self._mapping_port.transition(
            status=status, updated_at=datetime.now(UTC), **kw, **fields
        )
        self._snapshots[(hid, ref)] = res

    async def create_upload_authorization(
        self, request: UploadAuthorizationRequest, *, base_staging_url: str | None = None
    ) -> UploadAuthorizationResponse:
        """Register ALLOCATED mapping in Cosmos and issue short-lived upload token."""
        hid, ctype, sz = request.household_id, str(request.content_type), request.file_size_bytes
        media_id, now, exp_ts = uuid.uuid4().hex, datetime.now(UTC), time.time() + 300
        ext = self._determine_extension(request.filename, request.content_type)
        image_ref = f"blob://households/{hid}/media/{media_id}{ext}"
        base = (self._configured_base_url or base_staging_url or self._base_staging_url).rstrip("/")
        token, exp_dt = f"staging-token-{media_id}", now + timedelta(seconds=300)
        info = {"input_id": media_id, "household_id": hid, "image_ref": image_ref}
        self._pending_tokens[token] = info | {"content_type": ctype, "expires_at": exp_ts}
        self._init_retention(hid, image_ref, media_id, now)
        if self._mapping_port is not None:
            m_kw = info | {"content_type": ctype, "expected_file_size_bytes": sz}
            t_kw = {"upload_authorization_expires_at": exp_dt, "created_at": now, "updated_at": now}
            mapping = MediaInputMapping(status=MediaProcessingStatus.ALLOCATED, **m_kw, **t_kw)
            snap = await self._mapping_port.create_mapping(
                mapping, orphan_ttl_seconds=ORPHAN_TTL_SECONDS
            )
            self._snapshots[(hid, image_ref)] = snap
        hdrs = {"Content-Type": ctype, "x-ms-blob-type": "BlockBlob"}
        resp_kw = {"upload_url": f"{base}/{token}", "image_ref": image_ref}
        return UploadAuthorizationResponse(expires_in_seconds=300, required_headers=hdrs, **resp_kw)

    async def _fail_on_upload_error(
        self, hid: str, image_ref: str, reason: MediaFailureReason
    ) -> None:
        if self._mapping_port is not None:
            try:
                await self.fail_processing(hid, image_ref, reason)
            except Exception as exc:
                logger.error("Failed recording %s for %s: %s", reason, image_ref, exc)
                raise

    async def receive_staged_upload(
        self, upload_token: str, payload_bytes: bytes
    ) -> tuple[str, str]:
        """Validate token and payload, store media bytes, and run ingest pipeline if enabled."""
        info = self._pending_tokens.get(upload_token)
        if not info:
            raise KeyError(f"Upload token '{upload_token}' not found or already consumed")
        hid, image_ref, input_id = info["household_id"], info["image_ref"], info["input_id"]
        if time.time() > info["expires_at"]:
            self._pending_tokens.pop(upload_token, None)
            await self._fail_on_upload_error(hid, image_ref, MediaFailureReason.UPLOAD_TIMEOUT)
            raise TimeoutError(f"Upload token '{upload_token}' has expired")
        if not payload_bytes or len(payload_bytes) > MAX_UPLOAD_BYTES:
            await self._fail_on_upload_error(hid, image_ref, MediaFailureReason.VALIDATION_FAILED)
            if not payload_bytes:
                raise ValueError("Payload cannot be empty")
            raise ValueError(f"Payload exceeds maximum allowed size of 10MB: {len(payload_bytes)}")

        rec = self._init_retention(hid, image_ref, input_id, datetime.now(UTC))
        ctype = info.get("content_type", "image/jpeg")
        await self._store_raw_media(hid, image_ref, payload_bytes, ctype, rec)
        self._pending_tokens.pop(upload_token, None)
        sha = hashlib.sha256(payload_bytes).hexdigest()
        meta = {"file_size_bytes": len(payload_bytes), "sha256_hash": sha}
        await self._transition_mapping(hid, image_ref, MediaProcessingStatus.RECEIVED, **meta)
        if self._auto_process:
            await self._run_ingest_analysis(hid, image_ref)
        return (hid, image_ref)

    def _resolve_vision_service(self) -> Any | None:
        svc = self._vision_service
        try:
            return svc() if callable(svc) else svc
        except Exception:
            return None

    async def _run_ingest_analysis(self, hid: str, ref: str) -> None:
        from app.services.vision import MockVisionService

        svc = self._resolve_vision_service() or MockVisionService()
        result = await svc.analyze_image(hid, ref)
        snap = self._snapshots.get((hid, ref)) or await self.get_media_snapshot(hid, ref)
        if snap is None or snap.record.status != MediaProcessingStatus.COMPLETED:
            if snap is not None and snap.record.status == MediaProcessingStatus.RECEIVED:
                await self.begin_processing(hid, ref)
            await self.complete_processing(hid, ref, result.model_dump_json())

    async def begin_processing(self, household_id: str, image_ref: str) -> None:
        """Advance media mapping state to PROCESSING with COMPLETED idempotency guard."""
        if self._mapping_port is not None:
            snap = await self._get_snapshot(household_id, image_ref)
            if snap.record.status != MediaProcessingStatus.COMPLETED:
                await self._transition_mapping(
                    household_id, image_ref, MediaProcessingStatus.PROCESSING
                )

    async def complete_processing(
        self, household_id: str, image_ref: str, artifact_data: str | bytes | None = None
    ) -> str:
        """Persist derived artifact and advance media mapping state to COMPLETED."""
        hid, ref, now = household_id, image_ref, datetime.now(UTC)
        snap = await self._get_snapshot(hid, ref) if self._mapping_port else None
        done = snap is not None and snap.record.status == MediaProcessingStatus.COMPLETED
        if not done and not artifact_data:
            raise ValueError("Derived artifact_data is required before COMPLETED transition.")
        iid = snap.record.input_id if snap else "mock"
        rec = self._retention_records.get((hid, ref)) or self._init_retention(hid, ref, iid, now)
        input_id = snap.record.input_id if snap else str(rec.get("input_id") or "mock")
        art_ref = f"blob://households/{hid}/media/{input_id}/derived/vision_v1.json"
        if artifact_data:
            raw = artifact_data.encode("utf-8") if isinstance(artifact_data, str) else artifact_data
            await self._store_artifact_bytes(hid, ref, art_ref, raw, rec)
            rec["artifact_ref"] = art_ref
        if done and snap is not None:
            return snap.record.artifact_ref or art_ref
        st = MediaProcessingStatus.COMPLETED
        await self._transition_mapping(hid, ref, st, artifact_ref=art_ref)
        return art_ref

    async def fail_processing(
        self, household_id: str, image_ref: str, failure_reason: MediaFailureReason = DEFAULT_REASON
    ) -> None:
        """Advance media mapping state to FAILED with explicit failure reason."""
        st = MediaProcessingStatus.FAILED
        await self._transition_mapping(household_id, image_ref, st, failure_reason=failure_reason)

    async def retain_media(self, household_id: str, image_ref: str) -> bool:
        """Mark media and derived artifacts as attached so orphan cleanup preserves them."""
        rec = self._retention_records.get((household_id, image_ref))
        if rec is None or rec.get("purged"):
            return False
        rec["retention"], rec["expires_at"] = "attached", None
        await self._mark_storage_retained(household_id, image_ref, rec)
        return True

    async def cleanup_expired_blobs(self, now: datetime | None = None) -> list[str]:
        """Purge expired orphan media and derived artifacts while preserving attached media."""
        effective_now, purged = now or datetime.now(UTC), []
        for (hid, image_ref), rec in list(self._retention_records.items()):
            exp = rec.get("expires_at")
            if rec.get("purged") or rec.get("retention") == "attached":
                continue
            if exp is None or effective_now < exp:
                continue
            snap = await self.get_media_snapshot(hid, image_ref)
            if snap is not None and snap.record.ttl_seconds is None:
                await self.retain_media(hid, image_ref)
                continue
            await self._delete_media_and_artifact(hid, image_ref, rec)
            rec["purged"] = True
            self._snapshots.pop((hid, image_ref), None)
            purged.append(image_ref)
        return purged

    async def get_media_snapshot(
        self, household_id: str, image_ref: str
    ) -> MediaMappingSnapshot | None:
        """Retrieve authoritative MediaMappingSnapshot for downstream atomic retention."""
        port = self._mapping_port
        if port is None:
            return None
        return await port.find_by_image_ref(household_id=household_id, image_ref=image_ref)

    async def _get_snapshot(self, hid: str, ref: str) -> MediaMappingSnapshot:
        snap = self._snapshots.get((hid, ref)) or await self.get_media_snapshot(hid, ref)
        if snap is None:
            raise KeyError(f"Media mapping not found for '{ref}' in '{hid}'.")
        return snap

    async def _store_raw_media(self, h: str, r: str, d: bytes, ct: str, rec: dict) -> None: ...
    async def _store_artifact_bytes(self, h: str, r: str, a: str, d: bytes, rec: dict) -> None: ...
    async def _mark_storage_retained(self, h: str, r: str, rec: dict[str, Any]) -> None: ...
    async def _delete_media_and_artifact(self, h: str, r: str, rec: dict[str, Any]) -> None: ...

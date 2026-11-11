"""[PROVISIONAL_STAGING] 本機記憶體模擬影像儲存適配器 (Local In-Memory Staging Adapter).

================================================================================
【過渡期模組宣告 / TEMPORARY PROVISIONAL STAGING】
- 狀態說明：目前 Azure Blob Storage 與 Cosmos DB 資料庫尚未建置 (Unprovisioned)。
- 開發目的：本模組提供本機開發與測試環境之「上傳授權生命週期」與「image_ref 格式生成」模擬。
- 正式切換：待 Azure 基礎設施部署完畢後，將以正式的 AzureBlobMediaStorage 替換本實作，
  呼叫端介面與領域模型完全相容，不受底層更迭影響。
================================================================================
"""

import time
import uuid
from pathlib import Path

from app.models.storage import (
    AllowedMediaContentType,
    UploadAuthorizationRequest,
    UploadAuthorizationResponse,
)


class LocalStagingMediaStorage:
    """In-memory staging adapter simulating cloud blob direct-upload lifecycle.

    [NOTE: TEMPORARY STAGING BEFORE CLOUD DATABASE & BLOB DEPLOYMENT]
    """

    def __init__(self, base_staging_url: str | None = None) -> None:
        self._configured_base_url = base_staging_url
        # In-memory registry tracking staged items: (household_id, image_ref) -> bytes
        self._staged_media: dict[tuple[str, str], bytes] = {}
        # In-memory registry tracking pending authorization tokens
        self._pending_tokens: dict[str, dict] = {}

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
        """Simulate issuing a pre-signed SAS upload authorization."""
        media_id = uuid.uuid4().hex
        ext = self._determine_extension(request.filename, request.content_type)
        # Format adheres strictly to agreed opaque reference standard
        image_ref = f"blob://households/{request.household_id}/media/{media_id}{ext}"

        effective_base = (
            self._configured_base_url or base_staging_url or "http://127.0.0.1:8000/staging/upload"
        ).rstrip("/")
        upload_token = f"staging-token-{media_id}"
        upload_url = f"{effective_base}/{upload_token}"

        self._pending_tokens[upload_token] = {
            "household_id": request.household_id,
            "image_ref": image_ref,
            "content_type": str(request.content_type),
            "expires_at": time.time() + 300,
            "file_size_bytes": request.file_size_bytes,
        }

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

        if time.time() > token_info["expires_at"]:
            self._pending_tokens.pop(upload_token, None)
            raise TimeoutError(f"Upload token '{upload_token}' has expired")

        if len(payload_bytes) == 0:
            raise ValueError("Payload cannot be empty")

        if len(payload_bytes) > 10_485_760:
            raise ValueError(
                f"Payload exceeds maximum allowed size of 10MB: {len(payload_bytes)} bytes"
            )

        household_id = token_info["household_id"]
        image_ref = token_info["image_ref"]
        self._staged_media[(household_id, image_ref)] = payload_bytes
        self._pending_tokens.pop(upload_token, None)
        return (household_id, image_ref)

    async def verify_media_exists(self, household_id: str, image_ref: str) -> bool:
        """Verify if media exists in temporary staging for the specified household."""
        return (household_id, image_ref) in self._staged_media

    async def get_media_bytes(self, household_id: str, image_ref: str) -> bytes | None:
        """Retrieve media raw binary bytes if present for the specified household."""
        return self._staged_media.get((household_id, image_ref))

    async def simulate_client_upload(
        self, household_id: str, image_ref: str, payload_bytes: bytes
    ) -> None:
        """Helper method for local tests simulating client direct upload to storage."""
        self._staged_media[(household_id, image_ref)] = payload_bytes

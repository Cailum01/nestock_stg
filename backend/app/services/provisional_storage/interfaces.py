"""[PROVISIONAL_STAGING] 影像儲存服務抽象協定 (Media Storage Interface Protocol).

注意：
- 這是 A2 擁有的影像上傳授權與儲存驗證協定。
- 目前底層資料庫與 Azure Blob 尚未建立，未來正式實作將透過相同協定無縫置換。
"""

from datetime import datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from app.models.domain import MediaFailureReason
from app.models.storage import (
    UploadAuthorizationRequest,
    UploadAuthorizationResponse,
)

if TYPE_CHECKING:
    from app.services.media_mapping import MediaMappingSnapshot


@runtime_checkable
class MediaStorageService(Protocol):
    """Port for issuing upload authorization and verifying media persistence."""

    async def create_upload_authorization(
        self,
        request: UploadAuthorizationRequest,
        *,
        base_staging_url: str | None = None,
    ) -> UploadAuthorizationResponse:
        """Issue a short-lived, narrowly scoped upload authorization and reserve image_ref."""
        ...

    async def verify_media_exists(self, household_id: str, image_ref: str) -> bool:
        """Check whether media bytes exist and belong to the specified household."""
        ...

    async def get_media_bytes(self, household_id: str, image_ref: str) -> bytes | None:
        """Retrieve media raw binary bytes if present for the specified household."""
        ...

    async def get_derived_artifact(self, household_id: str, image_ref: str) -> bytes | None:
        """Retrieve persisted derived vision analysis artifact bytes if available."""
        ...

    async def begin_processing(self, household_id: str, image_ref: str) -> None:
        """Advance media mapping state to PROCESSING."""
        ...

    async def complete_processing(
        self,
        household_id: str,
        image_ref: str,
        artifact_data: str | bytes | None = None,
    ) -> str:
        """Advance media mapping state to COMPLETED and record derived artifact reference."""
        ...

    async def fail_processing(
        self,
        household_id: str,
        image_ref: str,
        failure_reason: MediaFailureReason = MediaFailureReason.PROCESSING_FAILED,
    ) -> None:
        """Advance media mapping state to FAILED with explicit failure reason."""
        ...

    async def retain_media(self, household_id: str, image_ref: str) -> bool:
        """Mark media and derived artifacts as attached to domain records to exempt orphan purge."""
        ...

    async def unretain_media(
        self, household_id: str, image_ref: str, ttl_seconds: int = 86400
    ) -> bool:
        """Release attached retention status, restoring orphan expiration window."""
        ...
    async def cleanup_expired_blobs(self, now: datetime | None = None) -> list[str]:
        """Purge expired orphan media and derived artifacts not attached to domain records."""
        ...

    async def get_media_snapshot(
        self, household_id: str, image_ref: str
    ) -> "MediaMappingSnapshot | None":
        """Retrieve the authoritative MediaMappingSnapshot for downstream atomic retention."""
        ...

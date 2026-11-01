"""[PROVISIONAL_STAGING] 影像儲存服務抽象協定 (Media Storage Interface Protocol).

注意：
- 這是 A2 擁有的影像上傳授權與儲存驗證協定。
- 目前底層資料庫與 Azure Blob 尚未建立，未來正式實作將透過相同協定無縫置換。
"""

from typing import Protocol, runtime_checkable

from app.models.storage import (
    UploadAuthorizationRequest,
    UploadAuthorizationResponse,
)


@runtime_checkable
class MediaStorageService(Protocol):
    """Port for issuing upload authorization and verifying media persistence."""

    async def create_upload_authorization(
        self, request: UploadAuthorizationRequest
    ) -> UploadAuthorizationResponse:
        """Issue a short-lived, narrowly scoped upload authorization and reserve image_ref."""
        ...

    async def verify_media_exists(self, household_id: str, image_ref: str) -> bool:
        """Check whether media bytes exist and belong to the specified household."""
        ...

    async def get_media_bytes(self, household_id: str, image_ref: str) -> bytes | None:
        """Retrieve media raw binary bytes if present for the specified household."""
        ...

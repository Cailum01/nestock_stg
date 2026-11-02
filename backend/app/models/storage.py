from enum import StrEnum

from pydantic import Field

from app.models.base import ContractModel


class AllowedMediaContentType(StrEnum):
    IMAGE_JPEG = "image/jpeg"
    IMAGE_PNG = "image/png"
    IMAGE_WEBP = "image/webp"


class UploadAuthorizationRequest(ContractModel):
    """Client request for uploading media bytes directly to storage."""

    household_id: str = Field(min_length=1)
    filename: str = Field(min_length=1)
    content_type: AllowedMediaContentType
    file_size_bytes: int = Field(gt=0, le=10_485_760)  # Max 10MB


class UploadAuthorizationResponse(ContractModel):
    """Scoped upload authorization token/URL and reserved opaque reference."""

    upload_url: str = Field(min_length=1)
    image_ref: str = Field(min_length=1)
    expires_in_seconds: int = Field(gt=0)
    required_headers: dict[str, str] = Field(default_factory=dict)

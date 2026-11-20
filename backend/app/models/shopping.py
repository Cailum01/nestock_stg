"""Shopping API request models."""

from pydantic import Field, field_validator

from app.models.base import ContractModel


class AddShoppingIntentRequest(ContractModel):
    """Create one pending Shopping row from the App's explicit Add action."""

    request_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    quantity: int | None = Field(default=None, ge=1, strict=True)
    unit: str | None = None

    @field_validator("request_id")
    @classmethod
    def _trimmed_request_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be empty")
        return cleaned

    @field_validator("name")
    @classmethod
    def _nonblank_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("unit")
    @classmethod
    def _nonblank_unit(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must not be empty when supplied")
        return value

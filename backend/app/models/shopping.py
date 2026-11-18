"""Shopping API request models."""

from typing import Self

from pydantic import Field, field_validator, model_validator

from app.models.base import ContractModel


class AddShoppingIntentRequest(ContractModel):
    """Create one pending Shopping row from the App's explicit Add action."""

    request_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    quantity: int | None = Field(default=None, ge=1, strict=True)
    unit: str | None = None

    @field_validator("request_id", "name")
    @classmethod
    def _trimmed_required(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be empty")
        return cleaned

    @field_validator("unit")
    @classmethod
    def _trimmed_unit(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @model_validator(mode="after")
    def _unit_not_blank(self) -> Self:
        if self.unit is not None and not self.unit:
            raise ValueError("unit must not be empty when supplied")
        return self

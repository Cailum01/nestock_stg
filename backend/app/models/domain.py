from datetime import date, datetime
from enum import StrEnum

from pydantic import Field, model_validator

from app.models.base import ContractModel


class InventoryItem(ContractModel):
    """Quantity is confirmed inventory; forecasts must never overwrite it."""

    item_id: str
    household_id: str
    name: str
    quantity: int
    unit: str
    created_at: datetime
    updated_at: datetime
    aliases: list[str] | None = None
    location: str | None = None
    expiry_date: date | None = None
    image_ref: str | None = None


class ShoppingStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"


class ShoppingItem(ContractModel):
    shopping_item_id: str
    household_id: str
    name: str
    quantity: int | None = None
    unit: str | None = None
    status: ShoppingStatus
    created_at: datetime
    updated_at: datetime
    item_id: str | None = None
    source: str | None = None
    product_url: str | None = None


class TransactionReason(StrEnum):
    PURCHASE = "purchase"
    CONSUMPTION = "consumption"
    ADJUSTMENT = "adjustment"


class Transaction(ContractModel):
    """B1 persists, B2 reads, C1 workflows may create; storage remains undecided."""

    transaction_id: str
    household_id: str
    item_id: str
    operation_id: str
    quantity_delta: int
    reason: TransactionReason
    created_at: datetime


class ChangeEvent(ContractModel):
    """Immutable provenance record for one committed purchase operation.

    PROVISIONAL per issue #60: B1 writes it in the same batch as the operation's
    domain writes. ``event_id`` equals ``operation_id`` so a retry maps to the
    same document. ``occurred_at`` is the purchase time; ``recorded_at`` is the
    approval decision time.
    """

    event_id: str
    household_id: str
    operation_id: str
    approval_id: str
    transaction_id: str
    shopping_item_id: str
    item_id: str
    occurred_at: datetime
    recorded_at: datetime


class MediaProcessingStatus(StrEnum):
    ALLOCATED = "allocated"
    RECEIVED = "received"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class MediaFailureReason(StrEnum):
    UPLOAD_TIMEOUT = "upload_timeout"
    VALIDATION_FAILED = "validation_failed"
    PROCESSING_FAILED = "processing_failed"


class MediaInputMapping(ContractModel):
    """Internal, household-scoped metadata for an opaque external media reference."""

    input_id: str = Field(min_length=1)
    household_id: str = Field(min_length=1)
    image_ref: str = Field(min_length=1)
    content_type: str = Field(min_length=1)
    expected_file_size_bytes: int = Field(gt=0)
    status: MediaProcessingStatus
    upload_authorization_expires_at: datetime
    created_at: datetime
    updated_at: datetime
    file_size_bytes: int | None = Field(default=None, gt=0)
    sha256_hash: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")
    artifact_ref: str | None = Field(default=None, min_length=1)
    failure_reason: MediaFailureReason | None = None
    ttl_seconds: int | None = Field(default=None, gt=0, exclude=True)

    @model_validator(mode="after")
    def validate_processing_state(self) -> "MediaInputMapping":
        if self.status is MediaProcessingStatus.ALLOCATED and any(
            value is not None
            for value in (
                self.file_size_bytes,
                self.sha256_hash,
                self.artifact_ref,
                self.failure_reason,
            )
        ):
            raise ValueError("Allocated media mappings cannot contain processing results.")
        if self.status in {
            MediaProcessingStatus.RECEIVED,
            MediaProcessingStatus.PROCESSING,
            MediaProcessingStatus.COMPLETED,
        } and (self.file_size_bytes is None or self.sha256_hash is None):
            raise ValueError("Received media states require file_size_bytes and sha256_hash.")
        if self.status is MediaProcessingStatus.COMPLETED and self.artifact_ref is None:
            raise ValueError("Completed media mappings require artifact_ref.")
        if self.status is MediaProcessingStatus.FAILED and self.failure_reason is None:
            raise ValueError("Failed media mappings require failure_reason.")
        if self.status is not MediaProcessingStatus.COMPLETED and self.artifact_ref is not None:
            raise ValueError("Only completed media mappings may have artifact_ref.")
        if self.status is not MediaProcessingStatus.FAILED and self.failure_reason is not None:
            raise ValueError("Only failed media mappings may have failure_reason.")
        return self

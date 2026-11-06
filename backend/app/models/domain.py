from datetime import date, datetime
from enum import StrEnum

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

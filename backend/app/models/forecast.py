from datetime import datetime
from enum import StrEnum

from pydantic import Field

from app.models.base import ContractModel


class ForecastStatus(StrEnum):
    ESTIMATED = "estimated"
    INSUFFICIENT_DATA = "insufficient_data"


class ForecastMethod(StrEnum):
    CONSUMPTION = "consumption"
    PURCHASE_INTERVAL = "purchase_interval"


class SuggestedAction(StrEnum):
    NONE = "none"
    CONSIDER_RESTOCK = "consider_restock"
    CONFIRM_QUANTITY = "confirm_quantity"


class ItemForecast(ContractModel):
    """B2 estimate for one item. PROVISIONAL; never a confirmed quantity.

    `confirmed_quantity` echoes InventoryItem.quantity as read; the forecast never
    writes it. CONFIRM_QUANTITY means the Agent may politely ask for the actual
    count, and any update still needs HITL approval.
    """

    household_id: str
    item_id: str
    status: ForecastStatus
    suggested_action: SuggestedAction
    confirmed_quantity: float
    unit: str
    generated_at: datetime
    consumption_event_count: int = Field(ge=0)
    purchase_event_count: int = Field(ge=0)
    method: ForecastMethod | None = None
    daily_consumption: float | None = Field(default=None, gt=0)
    days_until_empty: float | None = Field(default=None, ge=0)
    estimated_empty_at: datetime | None = None

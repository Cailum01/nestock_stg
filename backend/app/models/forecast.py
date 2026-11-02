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
    SUFFICIENCY_CHECK = "sufficiency_check"
    REFERENCE_SCALED = "reference_scaled"


class SuggestedAction(StrEnum):
    NONE = "none"
    CONSIDER_RESTOCK = "consider_restock"
    CHECK_SUFFICIENCY = "check_sufficiency"


class SufficiencyAnswer(StrEnum):
    ENOUGH = "enough"
    NOT_ENOUGH = "not_enough"


class SufficiencyCheck(ContractModel):
    """The user's answer to a "still enough?" card. PROVISIONAL.

    User-provided information about usage, not a quantity; it never changes
    InventoryItem.quantity. Storage and the write path still need B1/A1 agreement.
    """

    check_id: str
    household_id: str
    item_id: str
    answer: SufficiencyAnswer
    answered_at: datetime


class ItemForecast(ContractModel):
    """B2 estimate for one item. PROVISIONAL; never a confirmed quantity.

    `confirmed_quantity` echoes InventoryItem.quantity as read; the forecast never
    writes it. CHECK_SUFFICIENCY means the Agent shows a card asking whether the
    item is still enough; it never asks for household size, and any resulting
    quantity change still needs HITL approval. REFERENCE_SCALED estimates come
    from a reference rate times `household_factor`, not from the item's records.
    SUFFICIENCY_CHECK estimates bound how many days the last purchase lasted and
    give the window in which the next purchase is likely.
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
    household_factor: float | None = Field(default=None, gt=0)
    usage_days_min: float | None = Field(default=None, ge=0)
    usage_days_max: float | None = Field(default=None, gt=0)
    next_purchase_from: datetime | None = None
    next_purchase_to: datetime | None = None

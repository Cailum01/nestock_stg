from datetime import datetime, timezone
from typing import Literal

from pydantic import Field

from app.models.base import ContractModel

OptionSource = Literal["live", "demo"]


class PurchaseOption(ContractModel):
    """Provisional purchase option DTO for agent recommendations and C2 cards.

    Conforms to D-10 (contracts/open-decisions.md) & v8 section 12:
    - Each option has its own option_id; user selects one returning selected_option_id.
    - store: merchant/store name (e.g. 酷澎 Coupang, momo購物網).
    - source: data freshness indicator ('live' or 'demo'). Demo data must never pretend to be live.
    - updated_at: timestamp when price/option info was retrieved.
    - Invariants: price is only displayed for decision-making, never persisted to ShoppingItem.
    """

    option_id: str = Field(min_length=1, description="Unique option identity for selection")
    title: str = Field(min_length=1, description="Clean product title without marketing noise")
    raw_title: str | None = Field(default=None, description="Original raw title from provider")
    estimated_price: float | None = Field(
        default=None, ge=0.0, description="Current market price in TWD"
    )
    store: str = Field(min_length=1, description="Store name (e.g. 酷澎 Coupang, momo, PChome)")
    source: OptionSource = Field(description="Data freshness: 'live' or 'demo' (D-10)")
    product_url: str = Field(min_length=1, description="External referral deep-link URL")
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Timestamp when the option was fetched",
    )
    unit_spec: str | None = Field(default=None, description="Extracted package/unit specification")
    image_url: str | None = Field(default=None, description="Product thumbnail URL")

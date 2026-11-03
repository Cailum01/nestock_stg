from pydantic import Field

from app.models.base import ContractModel


class PurchaseOption(ContractModel):
    """Purchase option candidate for single-item comparison card (D-10 per v8 section 12).

    Invariants:
    - Entire comparison card shares one operation_id; each option has a distinct option_id.
    - source must explicitly distinguish 'live' vs 'demo'; demo data must not disguise as live.
    - price is ephemeral for display only, never persisted to ShoppingItem.
    """

    option_id: str = Field(min_length=1, description="Unique option identifier (e.g. opt-biggo-1)")
    title: str = Field(min_length=1, description="Normalized product title without noise")
    raw_title: str | None = Field(default=None, description="Original raw provider title")
    store: str = Field(min_length=1, description="Store/merchant name (e.g. momo, Coupang)")
    source: str = Field(min_length=1, description="Data source nature: strictly 'live' or 'demo'")
    estimated_price: float | None = Field(
        default=None, ge=0.0, description="Current market price in TWD"
    )
    product_url: str = Field(min_length=1, description="External referral deep-link URL")
    unit_spec: str | None = Field(default=None, description="Extracted package/unit specification")
    image_url: str | None = Field(default=None, description="Product thumbnail URL")
    updated_at: str | None = Field(default=None, description="ISO timestamp of price data")

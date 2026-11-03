from pydantic import Field

from app.models.base import ContractModel


class PurchaseOption(ContractModel):
    """Provisional purchase option DTO for agent recommendations and C2 cards.

    Invariants:
    - Ephemeral recommendation only; price is never persisted to ShoppingItem.
    - product_url must be a valid http/https URL for external deep-linking.
    """

    title: str = Field(min_length=1, description="Clean product title without marketing noise")
    raw_title: str | None = Field(default=None, description="Original raw title from provider")
    estimated_price: float | None = Field(
        default=None, ge=0.0, description="Current market price in TWD"
    )
    source: str = Field(min_length=1, description="Source store name (e.g. momo, Shopee, Coupang)")
    product_url: str = Field(min_length=1, description="External referral deep-link URL")
    unit_spec: str | None = Field(default=None, description="Extracted package/unit specification")
    image_url: str | None = Field(default=None, description="Product thumbnail URL")


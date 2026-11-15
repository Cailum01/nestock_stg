from enum import StrEnum
from typing import Literal

from pydantic import Field

from app.models.base import ContractModel


class CommerceSearchStatus(StrEnum):
    """Domain outcome status for commerce search queries."""

    SUCCESS = "success"
    """Search completed successfully with at least one matching purchase option."""

    NO_MATCH = "no_match"
    """Search executed normally, but no matching options were found."""

    UNAVAILABLE = "unavailable"
    """External commerce provider failed, timed out, or is temporarily unreachable."""


class PurchaseOption(ContractModel):
    """Purchase option candidate for single-item comparison card (D-10 per v8 section 12).

    Invariants:
    - Entire comparison card shares one operation_id; each option has a distinct option_id.
    - source must explicitly distinguish 'live' vs 'demo'; demo data must not disguise as live.
    - price is ephemeral for display only, never persisted to ShoppingItem.
    """

    option_id: str = Field(min_length=1, description="Unique option identifier (e.g. opt-biggo-1)")
    title: str = Field(min_length=1, description="Normalized product title without noise")
    raw_title: str | None = Field(
        default=None, exclude=True, description="Original raw provider title (internal only)"
    )
    store: str = Field(min_length=1, description="Store/merchant name (e.g. momo, Coupang)")
    source: Literal["live", "demo"] = Field(
        description="Data source nature: strictly 'live' or 'demo'"
    )
    estimated_price: float | None = Field(
        default=None, ge=0.0, description="Current market price in TWD"
    )
    product_url: str = Field(min_length=1, description="External referral deep-link URL")
    unit_spec: str | None = Field(default=None, description="Extracted package/unit specification")
    image_url: str | None = Field(default=None, description="Product thumbnail URL")
    updated_at: str | None = Field(default=None, description="ISO timestamp of price data")


class CommerceSearchResponse(ContractModel):
    """Envelope response for GET /api/commerce/options."""

    status: CommerceSearchStatus = Field(
        description="Domain outcome status: success, no_match, or unavailable"
    )
    code: str = Field(description="Machine-readable status code")
    message: str = Field(description="Human-readable status description")
    options: list[PurchaseOption] = Field(
        default_factory=list, description="Candidate purchase options (up to 3)"
    )

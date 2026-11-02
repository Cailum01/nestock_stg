from pydantic import Field

from app.models.base import ContractModel


class NormalizedItem(ContractModel):
    """Normalized domain entity extracted from raw vision or OCR observation.

    Invariants:
    - Never mutates domain inventory directly without HITL approval.
    - feature_keywords serves as exact snapshot for Transaction and Cosmos indexing.
    - unit strictly conforms to canonical household units.
    """

    raw_text: str = Field(min_length=1)
    canonical_name: str = Field(min_length=1)
    brand: str | None = None
    feature_keywords: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    quantity: float = Field(gt=0)
    unit: str | None = None
    unit_confirmed: bool = True
    capacity_spec: str | None = None

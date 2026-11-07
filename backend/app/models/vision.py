from datetime import date
from enum import StrEnum

from pydantic import Field

from app.models.base import ContractModel


class MatchStatus(StrEnum):
    EXACT_MATCH = "exact_match"
    ALIAS_MATCH = "alias_match"
    FUZZY_CANDIDATE = "fuzzy_candidate"
    NEW_ITEM = "new_item"


class SceneType(StrEnum):
    SINGLE_ITEM = "single_item"
    PANTRY_SCENE = "pantry_scene"
    RECEIPT = "receipt"


class CandidateMatch(ContractModel):
    """Candidate inventory item for fuzzy matching in HITL loop."""

    item_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    score: float = Field(ge=0.0, le=1.0)


class DetectedItem(ContractModel):
    """Inferred observation from vision processing.

    Invariants:
    - Never overwrites confirmed inventory quantity directly.
    - Confidence is strictly bounded between 0.0 and 1.0.
    """

    raw_detected_name: str = Field(min_length=1)
    canonical_name: str = Field(min_length=1)
    feature_keywords: list[str] = Field(default_factory=list)
    quantity: float = Field(gt=0)
    unit: str | None = None
    unit_confirmed: bool = True
    confidence: float = Field(ge=0.0, le=1.0)
    expiry_date: date | None = None
    matched_inventory_id: str | None = None
    candidate_matches: list[CandidateMatch] = Field(default_factory=list)
    match_status: MatchStatus = MatchStatus.NEW_ITEM


class VisionAnalysisResult(ContractModel):
    """Encapsulates structured extraction results from an opaque image reference."""

    image_ref: str = Field(min_length=1)
    scene_type: SceneType
    detected_items: list[DetectedItem] = Field(default_factory=list)
    raw_text: str | None = None

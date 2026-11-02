from typing import Protocol, runtime_checkable

from app.models.vision import (
    DetectedItem,
    MatchStatus,
    SceneType,
    VisionAnalysisResult,
)
from app.services.interfaces import InventoryService


@runtime_checkable
class VisionService(Protocol):
    """Port for multimodal vision inference and item normalization."""

    async def analyze_image(
        self,
        household_id: str,
        image_ref: str,
        scene_type: SceneType = SceneType.SINGLE_ITEM,
    ) -> VisionAnalysisResult: ...


class MockVisionService:
    """Mock vision provider for local development and deterministic testing."""

    def __init__(self, inventory_service: InventoryService) -> None:
        self.inventory_service = inventory_service

    async def analyze_image(
        self,
        household_id: str,
        image_ref: str,
        scene_type: SceneType = SceneType.SINGLE_ITEM,
    ) -> VisionAnalysisResult:
        if not image_ref or not image_ref.strip():
            raise ValueError("image_ref must be a non-empty string reference.")

        existing_items = await self.inventory_service.list_items(household_id)

        # Baseline mock recognition scenario
        raw_name = "統一陽光高纖無糖豆漿 450ml"
        canonical_name = "高纖無糖豆漿"
        keywords = ["豆漿", "無糖", "高纖"]

        # Match against inventory
        matched_id: str | None = None
        match_status = MatchStatus.NEW_ITEM

        for item in existing_items:
            if item.name == canonical_name:
                matched_id = item.item_id
                match_status = MatchStatus.EXACT_MATCH
                break
            if item.aliases and canonical_name in item.aliases:
                matched_id = item.item_id
                match_status = MatchStatus.ALIAS_MATCH
                break

        item = DetectedItem(
            raw_detected_name=raw_name,
            canonical_name=canonical_name,
            feature_keywords=keywords,
            quantity=1.0,
            unit="瓶",
            confidence=0.95,
            matched_inventory_id=matched_id,
            match_status=match_status,
        )

        return VisionAnalysisResult(
            image_ref=image_ref.strip(),
            scene_type=scene_type,
            detected_items=[item],
            raw_text=raw_name,
        )

from datetime import date

import pytest
from pydantic import ValidationError

from app.models.domain import InventoryItem
from app.models.vision import (
    DetectedItem,
    MatchStatus,
    SceneType,
    VisionAnalysisResult,
)
from app.services.vision import MockVisionService, VisionService


class DummyInventoryService:
    def __init__(self, items: list[InventoryItem]):
        self._items = items

    async def list_items(self, household_id: str) -> list[InventoryItem]:
        return [item for item in self._items if item.household_id == household_id]


def sample_detected_item() -> dict:
    return {
        "raw_detected_name": "統一陽光高纖無糖豆漿 450ml",
        "canonical_name": "高纖無糖豆漿",
        "feature_keywords": ["豆漿", "無糖", "高纖"],
        "quantity": 1.0,
        "unit": "瓶",
        "confidence": 0.95,
        "expiry_date": date(2026, 12, 31),
        "matched_inventory_id": "soy-001",
        "match_status": MatchStatus.EXACT_MATCH,
    }


def test_vision_model_validation_and_json_roundtrip():
    item = DetectedItem(**sample_detected_item())
    result = VisionAnalysisResult(
        image_ref="blob://households/demo/media/test.jpg",
        scene_type=SceneType.SINGLE_ITEM,
        detected_items=[item],
        raw_text="統一陽光高纖無糖豆漿",
    )
    dumped = result.model_dump_json()
    loaded = VisionAnalysisResult.model_validate_json(dumped)
    assert loaded == result
    assert loaded.detected_items[0].canonical_name == "高纖無糖豆漿"


@pytest.mark.parametrize("extra", [{"price": 35}, {"bounding_box": [0, 0, 10, 10]}])
def test_vision_models_reject_unagreed_fields(extra):
    with pytest.raises(ValidationError):
        DetectedItem(**(sample_detected_item() | extra))


@pytest.mark.parametrize("confidence", [-0.1, 1.01])
def test_detected_item_confidence_range(confidence):
    with pytest.raises(ValidationError):
        DetectedItem(**(sample_detected_item() | {"confidence": confidence}))


def test_detected_item_rejects_empty_names():
    with pytest.raises(ValidationError):
        DetectedItem(**(sample_detected_item() | {"raw_detected_name": ""}))
    with pytest.raises(ValidationError):
        DetectedItem(**(sample_detected_item() | {"canonical_name": ""}))


def test_vision_service_protocol_conformance():
    service = MockVisionService(inventory_service=DummyInventoryService([]))
    assert isinstance(service, VisionService)


@pytest.mark.anyio
async def test_mock_vision_service_matches_existing_item(sample_now=None):
    from datetime import UTC, datetime

    now = datetime(2026, 1, 1, tzinfo=UTC)
    existing_item = InventoryItem(
        item_id="soy-001",
        household_id="demo-household",
        name="高纖無糖豆漿",
        quantity=2.0,
        unit="瓶",
        created_at=now,
        updated_at=now,
        aliases=["無糖豆漿"],
    )
    service = MockVisionService(inventory_service=DummyInventoryService([existing_item]))
    result = await service.analyze_image(
        household_id="demo-household",
        image_ref="blob://households/demo-household/media/item1.jpg",
        scene_type=SceneType.SINGLE_ITEM,
    )
    assert len(result.detected_items) >= 1
    first_item = result.detected_items[0]
    assert first_item.match_status == MatchStatus.EXACT_MATCH
    assert first_item.matched_inventory_id == "soy-001"


@pytest.mark.anyio
async def test_mock_vision_service_new_item_when_not_in_inventory():
    service = MockVisionService(inventory_service=DummyInventoryService([]))
    result = await service.analyze_image(
        household_id="demo-household",
        image_ref="blob://households/demo-household/media/item2.jpg",
        scene_type=SceneType.SINGLE_ITEM,
    )
    assert len(result.detected_items) >= 1
    first_item = result.detected_items[0]
    assert first_item.match_status == MatchStatus.NEW_ITEM
    assert first_item.matched_inventory_id is None


@pytest.mark.anyio
async def test_vision_service_rejects_empty_image_ref():
    service = MockVisionService(inventory_service=DummyInventoryService([]))
    with pytest.raises(ValueError):
        await service.analyze_image(
            household_id="demo-household",
            image_ref="   ",
            scene_type=SceneType.SINGLE_ITEM,
        )

from datetime import date

import pytest
from pydantic import ValidationError

from app.core.dependencies import (
    _create_vision_service,
    _reset_vision_service_for_test,
    get_vision_service,
)
from app.models.domain import InventoryItem
from app.models.vision import (
    CandidateMatch,
    DetectedItem,
    MatchStatus,
    SceneType,
    VisionAnalysisResult,
)
from app.services.provisional_storage.local_staging_adapter import LocalStagingMediaStorage
from app.services.vision import (
    AzureOpenAIVisionService,
    MockVisionService,
    VisionOpenAISettings,
    VisionService,
    load_vision_azure_settings,
)


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
        "unit_confirmed": True,
        "confidence": 0.95,
        "expiry_date": date(2026, 12, 31),
        "matched_inventory_id": "soy-001",
        "candidate_matches": [{"item_id": "soy-001", "name": "高纖無糖豆漿", "score": 0.95}],
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
    assert len(loaded.detected_items[0].candidate_matches) == 1
    assert loaded.detected_items[0].candidate_matches[0].item_id == "soy-001"


def test_detected_item_allows_none_unit_and_unconfirmed():
    data = sample_detected_item()
    data["unit"] = None
    data["unit_confirmed"] = False
    item = DetectedItem(**data)
    assert item.unit is None
    assert item.unit_confirmed is False


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


def test_candidate_match_validation():
    cm = CandidateMatch(item_id="item-123", name="醬油", score=0.88)
    assert cm.item_id == "item-123"
    assert cm.score == 0.88

    with pytest.raises(ValidationError):
        CandidateMatch(item_id="", name="醬油", score=0.88)
    with pytest.raises(ValidationError):
        CandidateMatch(item_id="item-123", name="醬油", score=1.2)


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
async def test_mock_vision_service_fuzzy_candidate_matching():
    from datetime import UTC, datetime

    now = datetime(2026, 1, 1, tzinfo=UTC)
    # 建立兩個名稱不同但包含「豆漿」關鍵字的既有品項
    item1 = InventoryItem(
        item_id="soy-black",
        household_id="demo-household",
        name="統一黑豆漿",
        quantity=1.0,
        unit="瓶",
        created_at=now,
        updated_at=now,
    )
    item2 = InventoryItem(
        item_id="soy-organic",
        household_id="demo-household",
        name="義美有機無糖豆漿",
        quantity=3.0,
        unit="瓶",
        created_at=now,
        updated_at=now,
    )
    service = MockVisionService(inventory_service=DummyInventoryService([item1, item2]))
    # 辨識圖片為 "高纖無糖豆漿" (關鍵字: 豆漿, 無糖, 高纖)，非完全匹配但命中上述兩者特徵
    result = await service.analyze_image(
        household_id="demo-household",
        image_ref="blob://households/demo-household/media/item-fuzzy.jpg",
    )
    assert len(result.detected_items) >= 1
    detected = result.detected_items[0]
    assert detected.match_status == MatchStatus.FUZZY_CANDIDATE
    assert len(detected.candidate_matches) == 2
    candidate_ids = {c.item_id for c in detected.candidate_matches}
    assert candidate_ids == {"soy-black", "soy-organic"}


@pytest.mark.anyio
async def test_mock_vision_service_optional_scene_type():
    service = MockVisionService(inventory_service=DummyInventoryService([]))
    result = await service.analyze_image(
        household_id="demo-household",
        image_ref="blob://households/demo-household/media/item2.jpg",
        scene_type=None,
    )
    assert result.scene_type == SceneType.SINGLE_ITEM
    assert len(result.detected_items) >= 1


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
    assert len(first_item.candidate_matches) == 0


@pytest.mark.anyio
async def test_vision_service_rejects_empty_image_ref():
    service = MockVisionService(inventory_service=DummyInventoryService([]))
    with pytest.raises(ValueError):
        await service.analyze_image(
            household_id="demo-household",
            image_ref="   ",
            scene_type=SceneType.SINGLE_ITEM,
        )


def test_load_vision_azure_settings_fallback_to_brain_config():
    env = {
        "AZURE_OPENAI_ENDPOINT": "https://test.openai.azure.com/",
        "AZURE_OPENAI_API_KEY": "brain-key-123",
        "AZURE_OPENAI_DEPLOYMENT": "gpt-4o",
    }
    settings = load_vision_azure_settings(environment=env)
    assert settings is not None
    assert settings.endpoint == "https://test.openai.azure.com/"
    assert settings.api_key == "brain-key-123"
    assert settings.deployment == "gpt-4o"
    assert settings.base_url == "https://test.openai.azure.com/openai/v1/"


def test_load_vision_azure_settings_dedicated_vision_config():
    env = {
        "AZURE_OPENAI_ENDPOINT": "https://brain.openai.azure.com/",
        "AZURE_OPENAI_API_KEY": "brain-key",
        "AZURE_OPENAI_VISION_ENDPOINT": "https://vision.openai.azure.com/",
        "AZURE_OPENAI_VISION_API_KEY": "vision-key",
        "AZURE_OPENAI_VISION_DEPLOYMENT": "Phi-4-multimodal-instruct",
    }
    settings = load_vision_azure_settings(environment=env)
    assert settings is not None
    assert settings.endpoint == "https://vision.openai.azure.com/"
    assert settings.api_key == "vision-key"
    assert settings.deployment == "Phi-4-multimodal-instruct"


def test_load_vision_azure_settings_returns_none_when_empty():
    assert load_vision_azure_settings(environment={}) is None


def test_load_vision_azure_settings_does_not_mutate_os_environ():
    import os

    marker = "TEST_ISOLATION_SENTINEL_NESTOCK"
    if marker in os.environ:
        del os.environ[marker]

    load_vision_azure_settings(environment={})
    assert marker not in os.environ


def test_create_vision_service_falls_back_to_mock():
    inventory = DummyInventoryService([])
    media = LocalStagingMediaStorage()
    service = _create_vision_service(inventory, media, environment={})
    assert isinstance(service, MockVisionService)


def test_create_vision_service_creates_azure_adapter_when_configured():
    inventory = DummyInventoryService([])
    media = LocalStagingMediaStorage()
    env = {
        "AZURE_OPENAI_ENDPOINT": "https://test.openai.azure.com/",
        "AZURE_OPENAI_API_KEY": "secret",
    }
    service = _create_vision_service(inventory, media, environment=env)
    assert isinstance(service, AzureOpenAIVisionService)


def test_dependencies_get_vision_service():
    _reset_vision_service_for_test()
    service = get_vision_service()
    assert isinstance(service, VisionService)
    # Subsequent calls should return the same singleton instance
    assert get_vision_service() is service


def test_dependencies_get_vision_service_lazy_resolution_with_env():
    _reset_vision_service_for_test()
    env = {
        "AZURE_OPENAI_VISION_ENDPOINT": "https://test-vision.openai.azure.com/",
        "AZURE_OPENAI_VISION_API_KEY": "vision-secret",
    }
    custom_service = get_vision_service(environment=env)
    assert isinstance(custom_service, AzureOpenAIVisionService)

    # Calling without environment after reset should fallback to Mock if env not set
    _reset_vision_service_for_test()
    fallback_service = get_vision_service(environment={})
    assert isinstance(fallback_service, MockVisionService)
    _reset_vision_service_for_test()


@pytest.mark.anyio
async def test_azure_vision_service_offline_fallback():
    settings = VisionOpenAISettings(
        endpoint="https://fake.openai.azure.com/",
        api_key="",
        deployment="Phi-4-multimodal-instruct",
    )
    inventory = DummyInventoryService([])
    media = LocalStagingMediaStorage()
    service = AzureOpenAIVisionService(settings, inventory, media, mock_fallback=True)

    result = await service.analyze_image(
        household_id="demo-household",
        image_ref="blob://demo/test.jpg",
    )
    assert result.scene_type == SceneType.SINGLE_ITEM
    assert len(result.detected_items) >= 1
    assert result.detected_items[0].canonical_name == "高纖無糖豆漿"


@pytest.mark.anyio
async def test_azure_vision_service_ignores_empty_raw_name_items():
    from unittest.mock import AsyncMock, MagicMock, patch

    settings = VisionOpenAISettings(
        endpoint="https://fake.openai.azure.com/",
        api_key="valid-test-key",
        deployment="gpt-4o",
    )
    inventory = DummyInventoryService([])
    media = LocalStagingMediaStorage()
    await media.simulate_client_upload(
        "demo-household", "blob://demo/test.jpg", b"fake-image-bytes"
    )
    service = AzureOpenAIVisionService(settings, inventory, media, mock_fallback=False)

    fake_response_json = {
        "choices": [
            {
                "message": {
                    "content": """{
                        "scene_type": "single_item",
                        "raw_text": "可口可樂",
                        "items": [
                            {"raw_name": ""},
                            {"raw_name": "   "},
                            {"raw_name": "可口可樂 330ml", "quantity": 1.0, "unit": "罐"}
                        ]
                    }"""
                }
            }
        ]
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fake_response_json

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        result = await service.analyze_image(
            household_id="demo-household",
            image_ref="blob://demo/test.jpg",
        )

    assert result.scene_type == SceneType.SINGLE_ITEM
    # Empty ghost items must be safely filtered out
    assert len(result.detected_items) == 1
    assert result.detected_items[0].raw_detected_name == "可口可樂 330ml"
    assert result.detected_items[0].canonical_name == "可口可樂"
    assert result.detected_items[0].quantity == 1.0
    assert result.detected_items[0].unit == "罐"
    assert result.detected_items[0].unit_confirmed is True


@pytest.mark.anyio
async def test_azure_vision_service_raises_on_api_error_when_fallback_disabled():
    from unittest.mock import AsyncMock, MagicMock, patch

    settings = VisionOpenAISettings(
        endpoint="https://fake.openai.azure.com/",
        api_key="valid-test-key",
        deployment="gpt-4o",
    )
    inventory = DummyInventoryService([])
    media = LocalStagingMediaStorage()
    await media.simulate_client_upload(
        "demo-household", "blob://demo/fail.jpg", b"fake-image-bytes"
    )
    # mock_fallback defaults to False
    service = AzureOpenAIVisionService(settings, inventory, media)

    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Internal Server Error"

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        with pytest.raises(RuntimeError) as exc_info:
            await service.analyze_image(
                household_id="demo-household",
                image_ref="blob://demo/fail.jpg",
            )
        assert "Azure OpenAI Vision inference failed" in str(exc_info.value)


@pytest.mark.anyio
async def test_azure_vision_service_parses_dynamic_confidence():
    from unittest.mock import AsyncMock, MagicMock, patch

    settings = VisionOpenAISettings(
        endpoint="https://fake.openai.azure.com/",
        api_key="valid-test-key",
        deployment="gpt-4o",
    )
    inventory = DummyInventoryService([])
    media = LocalStagingMediaStorage()
    await media.simulate_client_upload(
        "demo-household", "blob://demo/conf.jpg", b"fake-image-bytes"
    )
    service = AzureOpenAIVisionService(settings, inventory, media, mock_fallback=False)

    fake_response_json = {
        "choices": [
            {
                "message": {
                    "content": """{
                        "scene_type": "pantry_scene",
                        "raw_text": "儲藏櫃",
                        "items": [
                            {
                                "raw_name": "義美全脂鮮奶 936ml",
                                "quantity": 1.0,
                                "unit": "瓶",
                                "confidence": 0.96
                            },
                            {
                                "raw_name": "模糊的罐頭",
                                "quantity": 2.0,
                                "unit": "罐",
                                "confidence": 0.42
                            }
                        ]
                    }"""
                }
            }
        ]
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fake_response_json

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        result = await service.analyze_image(
            household_id="demo-household",
            image_ref="blob://demo/conf.jpg",
        )

    assert result.scene_type == SceneType.PANTRY_SCENE
    assert len(result.detected_items) == 2
    assert result.detected_items[0].canonical_name == "全脂鮮奶"
    assert result.detected_items[0].confidence == 0.96
    assert result.detected_items[1].canonical_name == "模糊的罐頭"
    assert result.detected_items[1].confidence == 0.42

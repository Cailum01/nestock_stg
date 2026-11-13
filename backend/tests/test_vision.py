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
        "quantity": 1,
        "unit": "瓶",
        "unit_confirmed": True,
        "confidence": 0.95,
        "expiry_date": date(2026, 12, 31),
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
    assert loaded.detected_items[0].quantity == 1
    assert isinstance(loaded.detected_items[0].quantity, int)
    assert loaded.detected_items[0].matched_inventory_id is None
    assert loaded.detected_items[0].candidate_matches == []


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
async def test_mock_vision_service_returns_pure_observation_without_reading_inventory():
    class ProhibitiveInventoryService:
        async def list_items(self, household_id: str) -> list[InventoryItem]:
            raise AssertionError("A2 VisionService must never read inventory (Issue #44)")

    service = MockVisionService(inventory_service=ProhibitiveInventoryService())
    result = await service.analyze_image(
        household_id="demo-household",
        image_ref="blob://households/demo-household/media/item1.jpg",
        scene_type=SceneType.SINGLE_ITEM,
    )
    assert len(result.detected_items) == 1
    first_item = result.detected_items[0]
    assert first_item.canonical_name == "高纖無糖豆漿"
    assert first_item.quantity == 1
    assert isinstance(first_item.quantity, int)
    assert first_item.matched_inventory_id is None
    assert first_item.candidate_matches == []
    assert first_item.match_status == MatchStatus.NEW_ITEM


@pytest.mark.anyio
async def test_mock_vision_service_optional_scene_type():
    service = MockVisionService()
    result = await service.analyze_image(
        household_id="demo-household",
        image_ref="blob://households/demo-household/media/item2.jpg",
        scene_type=None,
    )
    assert result.scene_type == SceneType.SINGLE_ITEM
    assert len(result.detected_items) >= 1


@pytest.mark.anyio
async def test_vision_service_rejects_empty_image_ref():
    service = MockVisionService()
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


def test_create_vision_service_returns_none_when_unconfigured():
    inventory = DummyInventoryService([])
    media = LocalStagingMediaStorage()
    service = _create_vision_service(inventory, media, environment={})
    assert service is None


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
    env = {
        "AZURE_OPENAI_VISION_ENDPOINT": "https://test-vision.openai.azure.com/",
        "AZURE_OPENAI_VISION_API_KEY": "vision-secret",
    }
    service = get_vision_service(environment=env)
    assert isinstance(service, VisionService)
    _reset_vision_service_for_test()


def test_dependencies_get_vision_service_lazy_resolution_with_env():
    _reset_vision_service_for_test()
    env = {
        "AZURE_OPENAI_VISION_ENDPOINT": "https://test-vision.openai.azure.com/",
        "AZURE_OPENAI_VISION_API_KEY": "vision-secret",
    }
    custom_service = get_vision_service(environment=env)
    assert isinstance(custom_service, AzureOpenAIVisionService)

    # Calling with empty environment after reset should return None (no silent fallback to Mock)
    _reset_vision_service_for_test()
    fallback_service = get_vision_service(environment={})
    assert fallback_service is None
    _reset_vision_service_for_test()


@pytest.mark.anyio
async def test_real_agent_composition_without_vision_does_not_fake_success():
    _reset_vision_service_for_test()
    from app.agent.read_agent import ReadAgent, load_system_prompt
    from app.models.agent import AgentRequest, ResultKind
    from app.services.demo import DemoShoppingService
    from app.tools.queries import QueryTools
    from tests.test_read_agent import AIMessage, ScriptedModel

    model = ScriptedModel(AIMessage(content="照片辨識還沒開放，目前只能用文字查詢。"))
    tools = QueryTools(DummyInventoryService([]), DemoShoppingService())

    agent = ReadAgent(
        model=model,
        tools=tools,
        system_prompt=load_system_prompt(),
        vision=get_vision_service(environment={}),
        media=LocalStagingMediaStorage(),
    )

    request = AgentRequest(
        household_id="demo-household",
        message="這張照片裡有什麼？",
        image_ref="blob://households/demo-household/media/photo.jpg",
    )
    response = await agent.respond(request)

    assert not any(r.kind == ResultKind.VISION for r in response.results)


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
    assert result.detected_items[0].quantity == 1
    assert isinstance(result.detected_items[0].quantity, int)
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


def test_load_vision_azure_settings_temperature_default_none():
    env = {
        "AZURE_OPENAI_VISION_ENDPOINT": "https://test-vision.openai.azure.com/",
        "AZURE_OPENAI_VISION_API_KEY": "vision-secret",
    }
    settings = load_vision_azure_settings(environment=env)
    assert settings is not None
    assert settings.temperature is None


def test_load_vision_azure_settings_temperature_custom_float():
    env = {
        "AZURE_OPENAI_VISION_ENDPOINT": "https://test-vision.openai.azure.com/",
        "AZURE_OPENAI_VISION_API_KEY": "vision-secret",
        "AZURE_OPENAI_VISION_TEMPERATURE": "0.2",
    }
    settings = load_vision_azure_settings(environment=env)
    assert settings is not None
    assert settings.temperature == 0.2


def test_load_vision_azure_settings_temperature_invalid_string_defaults_none():
    env = {
        "AZURE_OPENAI_VISION_ENDPOINT": "https://test-vision.openai.azure.com/",
        "AZURE_OPENAI_VISION_API_KEY": "vision-secret",
        "AZURE_OPENAI_VISION_TEMPERATURE": "invalid-not-a-number",
    }
    settings = load_vision_azure_settings(environment=env)
    assert settings is not None
    assert settings.temperature is None


@pytest.mark.anyio
async def test_azure_vision_payload_omits_temperature_when_none():
    from unittest.mock import AsyncMock, MagicMock, patch

    settings = VisionOpenAISettings(
        endpoint="https://fake.openai.azure.com/",
        api_key="valid-test-key",
        deployment="gpt-4o",
        temperature=None,
    )
    inventory = DummyInventoryService([])
    media = LocalStagingMediaStorage()
    await media.simulate_client_upload(
        "demo-household", "blob://demo/payload-test.jpg", b"fake-image-bytes"
    )
    service = AzureOpenAIVisionService(settings, inventory, media)

    fake_response_json = {
        "choices": [
            {
                "message": {
                    "content": '{"scene_type": "single_item", "raw_text": "測試", "items": []}'
                }
            }
        ]
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fake_response_json

    with patch(
        "httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp
    ) as mock_post:
        await service.analyze_image("demo-household", "blob://demo/payload-test.jpg")
        call_kwargs = mock_post.call_args.kwargs
        payload = call_kwargs["json"]
        assert "temperature" not in payload


@pytest.mark.anyio
async def test_azure_vision_payload_includes_temperature_when_configured():
    from unittest.mock import AsyncMock, MagicMock, patch

    settings = VisionOpenAISettings(
        endpoint="https://fake.openai.azure.com/",
        api_key="valid-test-key",
        deployment="gpt-4o",
        temperature=0.2,
    )
    inventory = DummyInventoryService([])
    media = LocalStagingMediaStorage()
    await media.simulate_client_upload(
        "demo-household", "blob://demo/payload-test-2.jpg", b"fake-image-bytes"
    )
    service = AzureOpenAIVisionService(settings, inventory, media)

    fake_response_json = {
        "choices": [
            {
                "message": {
                    "content": '{"scene_type": "single_item", "raw_text": "測試", "items": []}'
                }
            }
        ]
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fake_response_json

    with patch(
        "httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp
    ) as mock_post:
        await service.analyze_image("demo-household", "blob://demo/payload-test-2.jpg")
        call_kwargs = mock_post.call_args.kwargs
        payload = call_kwargs["json"]
        assert payload.get("temperature") == 0.2


@pytest.mark.anyio
async def test_vision_mock_fallback_does_not_mask_complete_processing_error():
    """驗證當 mock_fallback=True 時，complete_processing 持久化錯誤向上拋出，絕不降級為 mock。"""
    from unittest.mock import AsyncMock, MagicMock, patch

    settings = VisionOpenAISettings(
        endpoint="https://fake.openai.azure.com/",
        api_key="valid-test-key",
        deployment="gpt-4o",
    )
    inventory = DummyInventoryService([])
    media = LocalStagingMediaStorage()
    await media.simulate_client_upload(
        "demo-household", "blob://demo/conflict.jpg", b"fake-image-bytes"
    )
    media.complete_processing = AsyncMock(
        side_effect=RuntimeError("Cosmos ETag conflict on complete_processing")
    )
    service = AzureOpenAIVisionService(settings, inventory, media, mock_fallback=True)

    fake_content = (
        '{"scene_type": "single_item", "raw_text": "鮮奶", '
        '"items": [{"raw_name": "鮮奶", "quantity": 1, "unit": "瓶"}]}'
    )
    fake_response_json = {"choices": [{"message": {"content": fake_content}}]}
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fake_response_json

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        with pytest.raises(RuntimeError, match="Cosmos ETag conflict on complete_processing"):
            await service.analyze_image("demo-household", "blob://demo/conflict.jpg")


@pytest.mark.anyio
async def test_vision_mock_fallback_does_not_mask_begin_processing_error():
    """驗證當 mock_fallback=True 時，begin_processing 持久化錯誤向上拋出，絕不降級為 mock。"""
    from unittest.mock import AsyncMock

    settings = VisionOpenAISettings(
        endpoint="https://fake.openai.azure.com/",
        api_key="valid-test-key",
        deployment="gpt-4o",
    )
    inventory = DummyInventoryService([])
    media = LocalStagingMediaStorage()
    await media.simulate_client_upload(
        "demo-household", "blob://demo/begin-fail.jpg", b"fake-image-bytes"
    )
    media.begin_processing = AsyncMock(
        side_effect=RuntimeError("Cosmos write failure on begin_processing")
    )
    service = AzureOpenAIVisionService(settings, inventory, media, mock_fallback=True)

    with pytest.raises(RuntimeError, match="Cosmos write failure on begin_processing"):
        await service.analyze_image("demo-household", "blob://demo/begin-fail.jpg")

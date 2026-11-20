import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.agent.llm import VertexAISettings, load_vertex_settings
from app.core.dependencies import (
    _create_vision_service,
    _reset_vision_service_for_test,
    get_vision_service,
)
from app.models.domain import InventoryItem
from app.models.vision import (
    CandidateMatch,
    DetectedItem,
    ImageComparisonResult,
    MatchStatus,
    SceneType,
    VisionAnalysisResult,
)
from app.services.provisional_storage.local_staging_adapter import LocalStagingMediaStorage
from app.services.vision import (
    AzureOpenAIVisionService,
    MockVisionService,
    VertexVisionService,
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


def test_create_vision_service_creates_vertex_adapter_when_project_configured(monkeypatch):
    inventory = DummyInventoryService([])
    media = LocalStagingMediaStorage()
    sentinel_client = object()
    monkeypatch.setattr("app.services.vision.build_vertex_client", lambda settings: sentinel_client)
    env = {"NESTOCK_VERTEX_PROJECT_ID": "test-project"}
    service = _create_vision_service(inventory, media, environment=env)
    assert isinstance(service, VertexVisionService)
    assert service._vertex_client is sentinel_client
    assert service.settings == VertexAISettings(project="test-project")


def test_dependencies_get_vision_service():
    _reset_vision_service_for_test()
    env = {"NESTOCK_VERTEX_PROJECT_ID": "test-project"}
    from app.services import vision

    vision_service_client = object()
    original = vision.build_vertex_client
    vision.build_vertex_client = lambda settings: vision_service_client
    try:
        service = get_vision_service(environment=env)
        assert isinstance(service, VisionService)
        assert isinstance(service, VertexVisionService)
    finally:
        vision.build_vertex_client = original
        _reset_vision_service_for_test()


def test_dependencies_get_vision_service_lazy_resolution_with_env():
    _reset_vision_service_for_test()
    from app.services import vision

    sentinel_client = object()
    original = vision.build_vertex_client
    vision.build_vertex_client = lambda settings: sentinel_client
    env = {"NESTOCK_VERTEX_PROJECT_ID": "test-project"}
    try:
        custom_service = get_vision_service(environment=env)
        assert isinstance(custom_service, VertexVisionService)

        # Empty injected configuration stays disabled rather than faking recognition.
        _reset_vision_service_for_test()
        fallback_service = get_vision_service(environment={})
        assert fallback_service is None
    finally:
        vision.build_vertex_client = original
        _reset_vision_service_for_test()


@pytest.mark.anyio
async def test_vertex_vision_uses_mocked_model_and_preserves_media_result():
    media = LocalStagingMediaStorage()
    household_id = "household-vertex-test"
    image_ref = f"blob://households/{household_id}/media/photo.jpg"
    await media.simulate_client_upload(household_id, image_ref, b"\xff\xd8\xff" + b"x" * 30)

    response_text = json.dumps(
        {
            "scene_type": "single_item",
            "raw_text": "豆漿",
            "items": [{"raw_name": "豆漿 450ml", "quantity": 1, "unit": "瓶", "confidence": 0.9}],
        }
    )
    generate_content = AsyncMock(return_value=SimpleNamespace(text=response_text))
    client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    )
    settings = load_vertex_settings(
        environment={
            "NESTOCK_VERTEX_PROJECT_ID": "test-project",
            "NESTOCK_VERTEX_MODEL": "gemini-3.8-flash",
        }
    )
    assert settings is not None
    service = VertexVisionService(
        settings=settings,
        media_storage=media,
        client=client,
    )

    result = await service.analyze_image(household_id, image_ref)

    assert result.image_ref == image_ref
    assert result.detected_items[0].canonical_name
    assert generate_content.await_count == 1
    assert generate_content.await_args.kwargs["model"] == "gemini-3.8-flash"
    assert await media.verify_media_exists(household_id, image_ref)
    assert await media.get_derived_artifact(household_id, image_ref) is not None


@pytest.mark.anyio
async def test_vertex_vision_comparison_uses_mocked_model_and_preserves_result_contract():
    media = LocalStagingMediaStorage()
    household_id = "household-compare-test"
    image_ref_a = f"blob://households/{household_id}/media/a.jpg"
    image_ref_b = f"blob://households/{household_id}/media/b.jpg"
    await media.simulate_client_upload(household_id, image_ref_a, b"\xff\xd8\xff" + b"a" * 24)
    await media.simulate_client_upload(household_id, image_ref_b, b"\xff\xd8\xff" + b"b" * 24)
    generate_content = AsyncMock(
        return_value=SimpleNamespace(
            text=(
                '{"is_same_item": "yes", "similarity_score": 0.92, "confidence": 0.88, '
                '"reasoning": "same product", "visual_differences": []}'
            )
        )
    )
    client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    )
    service = VertexVisionService(
        settings=VertexAISettings(project="test-project"),
        media_storage=media,
        client=client,
    )

    result = await service.compare_images(household_id, image_ref_a, image_ref_b)

    assert result.is_same_item is True
    assert result.similarity_score == 0.92
    assert result.confidence == 0.88
    assert result.visual_differences == []
    assert generate_content.await_count == 1


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


@pytest.mark.anyio
async def test_detect_image_mime_and_multiformat_vision_request():
    """驗證 detect_image_mime 支援 JPEG, PNG, WebP，且 _request_openai_vision 使用正確 MIME。"""
    from unittest.mock import AsyncMock, MagicMock, patch

    from app.services.vision import detect_image_mime

    jpeg_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF"
    png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    webp_bytes = b"RIFF\x1a\x00\x00\x00WEBPVP8 "
    unknown_bytes = b"\x00\x01\x02\x03\x04\x05"

    assert detect_image_mime(jpeg_bytes) == "image/jpeg"
    assert detect_image_mime(png_bytes) == "image/png"
    assert detect_image_mime(webp_bytes) == "image/webp"
    assert detect_image_mime(unknown_bytes) == "image/jpeg"

    settings = VisionOpenAISettings(
        endpoint="https://fake.openai.azure.com/",
        api_key="valid-test-key",
        deployment="gpt-4o",
    )
    inventory = DummyInventoryService([])
    media = LocalStagingMediaStorage()
    service = AzureOpenAIVisionService(settings, inventory, media)

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "choices": [{"message": {"content": '{"scene_type": "single_item", "items": []}'}}]
    }

    # 驗證 PNG 請求中 image_url 包含 data:image/png
    with patch(
        "httpx.AsyncClient.post", new_callable=AsyncMock, return_value=fake_resp
    ) as mock_post:
        await service._request_openai_vision(png_bytes, SceneType.SINGLE_ITEM)
        assert mock_post.called
        payload = mock_post.call_args.kwargs["json"]
        image_url = payload["messages"][1]["content"][1]["image_url"]["url"]
        assert image_url.startswith("data:image/png;base64,")

    # 驗證 WebP 請求中 image_url 包含 data:image/webp
    with patch(
        "httpx.AsyncClient.post", new_callable=AsyncMock, return_value=fake_resp
    ) as mock_post:
        await service._request_openai_vision(webp_bytes, SceneType.SINGLE_ITEM)
        assert mock_post.called
        payload = mock_post.call_args.kwargs["json"]
        image_url = payload["messages"][1]["content"][1]["image_url"]["url"]
        assert image_url.startswith("data:image/webp;base64,")


def test_image_comparison_result_validation():
    res = ImageComparisonResult(
        image_ref_a="blob://households/demo/media/a.jpg",
        image_ref_b="blob://households/demo/media/b.jpg",
        is_same_item=True,
        similarity_score=0.92,
        confidence=0.88,
        reasoning="Identical Soy Milk bottle",
        visual_differences=[],
    )
    dumped = res.model_dump_json()
    loaded = ImageComparisonResult.model_validate_json(dumped)
    assert loaded == res
    assert loaded.is_same_item is True
    assert loaded.similarity_score == 0.92

    # Reject empty image references
    with pytest.raises(ValidationError):
        ImageComparisonResult(
            image_ref_a="",
            image_ref_b="blob://households/demo/media/b.jpg",
            is_same_item=True,
            similarity_score=0.9,
            confidence=0.9,
        )

    # Reject out of bound scores
    with pytest.raises(ValidationError):
        ImageComparisonResult(
            image_ref_a="blob://households/demo/media/a.jpg",
            image_ref_b="blob://households/demo/media/b.jpg",
            is_same_item=True,
            similarity_score=1.5,
            confidence=0.9,
        )
    with pytest.raises(ValidationError):
        ImageComparisonResult(
            image_ref_a="blob://households/demo/media/a.jpg",
            image_ref_b="blob://households/demo/media/b.jpg",
            is_same_item=True,
            similarity_score=0.9,
            confidence=-0.1,
        )

    # Reject unagreed extra fields
    with pytest.raises(ValidationError):
        ImageComparisonResult(
            image_ref_a="blob://households/demo/media/a.jpg",
            image_ref_b="blob://households/demo/media/b.jpg",
            is_same_item=True,
            similarity_score=0.9,
            confidence=0.9,
            price=100,  # Forbidden extra field
        )


@pytest.mark.anyio
async def test_mock_vision_compare_images():
    service = MockVisionService()
    # 1. 相同引用短路
    same_res = await service.compare_images("demo-household", "blob://a.jpg", "blob://a.jpg")
    assert same_res.is_same_item is True
    assert same_res.similarity_score == 1.0
    assert same_res.confidence == 1.0
    assert same_res.visual_differences == []

    # 2. 相異引用確定性比較
    diff_res = await service.compare_images("demo-household", "blob://a.jpg", "blob://b.jpg")
    assert diff_res.is_same_item is False
    assert diff_res.similarity_score == 0.15
    assert diff_res.confidence == 0.95

    # 3. 空引用拋出例外
    with pytest.raises(ValueError):
        await service.compare_images("demo-household", "", "blob://b.jpg")
    with pytest.raises(ValueError):
        await service.compare_images("demo-household", "blob://a.jpg", "")


@pytest.mark.anyio
async def test_azure_vision_compare_images_short_circuit_and_missing_media():
    settings = VisionOpenAISettings(
        endpoint="https://fake.openai.azure.com/",
        api_key="valid-test-key",
        deployment="gpt-4o",
    )
    media = LocalStagingMediaStorage()
    service = AzureOpenAIVisionService(settings, media_storage=media, mock_fallback=False)

    # 不存在的相同圖片在無 mock_fallback 時拋錯
    with pytest.raises(ValueError, match="Media not found"):
        await service.compare_images("demo-household", "blob://missing.jpg", "blob://missing.jpg")

    # 模擬圖片存在
    await media.simulate_client_upload("demo-household", "blob://exists.jpg", b"fake-jpeg-bytes")
    res = await service.compare_images("demo-household", "blob://exists.jpg", "blob://exists.jpg")
    assert res.is_same_item is True
    assert res.similarity_score == 1.0

    # 缺其中一張圖拋錯
    with pytest.raises(ValueError, match="Media bytes not found for image_ref_b"):
        await service.compare_images("demo-household", "blob://exists.jpg", "blob://not-exists.jpg")


@pytest.mark.anyio
async def test_azure_vision_compare_images_mock_fallback_on_missing_creds():
    settings = VisionOpenAISettings(
        endpoint="",
        api_key="",
        deployment="gpt-4o",
    )
    media = LocalStagingMediaStorage()
    service = AzureOpenAIVisionService(settings, media_storage=media, mock_fallback=True)

    res = await service.compare_images("demo-household", "blob://a.jpg", "blob://b.jpg")
    assert res.is_same_item is False
    assert res.similarity_score == 0.15


@pytest.mark.anyio
async def test_azure_vision_compare_images_multimodal_api_success():
    from unittest.mock import AsyncMock, MagicMock, patch

    settings = VisionOpenAISettings(
        endpoint="https://fake.openai.azure.com/",
        api_key="valid-test-key",
        deployment="gpt-4o",
    )
    media = LocalStagingMediaStorage()
    await media.simulate_client_upload(
        "demo-household", "blob://a.jpg", b"\xff\xd8\xff\xe0\x00\x10JFIF"
    )
    await media.simulate_client_upload(
        "demo-household", "blob://b.jpg", b"\x89PNG\r\n\x1a\nfake-png"
    )
    service = AzureOpenAIVisionService(settings, media_storage=media, mock_fallback=False)

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": (
                        '{"is_same_item": true, "similarity_score": 0.94, "confidence": 0.90, '
                        '"reasoning": "Both bottles have matching brand logo and 450ml label.", '
                        '"visual_differences": []}'
                    )
                }
            }
        ]
    }

    with patch(
        "httpx.AsyncClient.post", new_callable=AsyncMock, return_value=fake_resp
    ) as mock_post:
        res = await service.compare_images("demo-household", "blob://a.jpg", "blob://b.jpg")
        assert mock_post.called
        payload = mock_post.call_args.kwargs["json"]
        user_content = payload["messages"][1]["content"]
        # Content has text prompt + 2 image_url objects
        assert len(user_content) == 3
        assert user_content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        assert user_content[2]["image_url"]["url"].startswith("data:image/png;base64,")

        assert res.is_same_item is True
        assert res.similarity_score == 0.94
        assert res.confidence == 0.90
        assert res.visual_differences == []
        assert "matching brand logo" in (res.reasoning or "")


@pytest.mark.anyio
async def test_azure_vision_compare_images_different_specs_scenario():
    from unittest.mock import AsyncMock, MagicMock, patch

    settings = VisionOpenAISettings(
        endpoint="https://fake.openai.azure.com/",
        api_key="valid-test-key",
        deployment="gpt-4o",
    )
    media = LocalStagingMediaStorage()
    await media.simulate_client_upload("demo-household", "blob://soy_450.jpg", b"fake-jpeg-1")
    await media.simulate_client_upload("demo-household", "blob://soy_900.jpg", b"fake-jpeg-2")
    service = AzureOpenAIVisionService(settings, media_storage=media, mock_fallback=False)

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": (
                        '{"is_same_item": false, "similarity_score": 0.60, "confidence": 0.85, '
                        '"reasoning": "Same brand line, but 450ml bottle vs 900ml family carton.", '
                        '"visual_differences": ["Packaging form factor differs", '
                        '"Net volume 450ml vs 900ml"]}'
                    )
                }
            }
        ]
    }

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=fake_resp):
        res = await service.compare_images(
            "demo-household", "blob://soy_450.jpg", "blob://soy_900.jpg"
        )
        assert res.is_same_item is False
        assert res.similarity_score == 0.60
        assert len(res.visual_differences) == 2


@pytest.mark.anyio
async def test_azure_vision_compare_images_bounding_defense_on_hallucinated_score():
    from unittest.mock import AsyncMock, MagicMock, patch

    settings = VisionOpenAISettings(
        endpoint="https://fake.openai.azure.com/",
        api_key="valid-test-key",
        deployment="gpt-4o",
    )
    media = LocalStagingMediaStorage()
    await media.simulate_client_upload("demo-household", "blob://a.jpg", b"fake-jpeg-1")
    await media.simulate_client_upload("demo-household", "blob://b.jpg", b"fake-jpeg-2")
    service = AzureOpenAIVisionService(settings, media_storage=media, mock_fallback=False)

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": (
                        '{"is_same_item": true, "similarity_score": 5.5, "confidence": -1.2, '
                        '"reasoning": "Out-of-bounds score hallucination.", '
                        '"visual_differences": []}'
                    )
                }
            }
        ]
    }

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=fake_resp):
        res = await service.compare_images("demo-household", "blob://a.jpg", "blob://b.jpg")
        # Ensure scores are clamped defensively to valid range [0.0, 1.0]
        assert res.similarity_score == 1.0
        assert res.confidence == 0.0


@pytest.mark.anyio
async def test_azure_vision_compare_images_string_false_boolean_defense():
    """驗證模型回傳字串 'false' 時防禦性解析為 False，杜絕誤判為同物品。"""
    from unittest.mock import AsyncMock, MagicMock, patch

    settings = VisionOpenAISettings(
        endpoint="https://fake.openai.azure.com/",
        api_key="valid-test-key",
        deployment="gpt-4o",
    )
    media = LocalStagingMediaStorage()
    await media.simulate_client_upload("demo-household", "blob://a.jpg", b"fake-jpeg-1")
    await media.simulate_client_upload("demo-household", "blob://b.jpg", b"fake-jpeg-2")
    service = AzureOpenAIVisionService(settings, media_storage=media, mock_fallback=False)

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": (
                        '{"is_same_item": "false", "similarity_score": 0.20, "confidence": 0.90, '
                        '"reasoning": "Completely different items.", "visual_differences": []}'
                    )
                }
            }
        ]
    }

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=fake_resp):
        res = await service.compare_images("demo-household", "blob://a.jpg", "blob://b.jpg")
        assert res.is_same_item is False
        assert res.similarity_score == 0.20


@pytest.mark.anyio
async def test_azure_vision_compare_images_nan_score_and_confidence_defense():
    """驗證模型回傳 NaN 浮點數時，自動回退安全預設值而非夾止為滿分 1.0。"""
    from unittest.mock import AsyncMock, MagicMock, patch

    settings = VisionOpenAISettings(
        endpoint="https://fake.openai.azure.com/",
        api_key="valid-test-key",
        deployment="gpt-4o",
    )
    media = LocalStagingMediaStorage()
    await media.simulate_client_upload("demo-household", "blob://a.jpg", b"fake-jpeg-1")
    await media.simulate_client_upload("demo-household", "blob://b.jpg", b"fake-jpeg-2")
    service = AzureOpenAIVisionService(settings, media_storage=media, mock_fallback=False)

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": (
                        '{"is_same_item": false, "similarity_score": "NaN", "confidence": "NaN", '
                        '"reasoning": "Malformed floating point values.", "visual_differences": []}'
                    )
                }
            }
        ]
    }

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=fake_resp):
        res = await service.compare_images("demo-household", "blob://a.jpg", "blob://b.jpg")
        assert res.is_same_item is False
        assert res.similarity_score == 0.1
        assert res.confidence == 0.5
        assert res.similarity_score != 1.0
        assert res.confidence != 1.0


@pytest.mark.anyio
async def test_azure_vision_compare_images_string_visual_differences_defense():
    """驗證模型回傳字串 'none' 而非清單時，防禦性轉換為空清單而非拆解為字元。"""
    from unittest.mock import AsyncMock, MagicMock, patch

    settings = VisionOpenAISettings(
        endpoint="https://fake.openai.azure.com/",
        api_key="valid-test-key",
        deployment="gpt-4o",
    )
    media = LocalStagingMediaStorage()
    await media.simulate_client_upload("demo-household", "blob://a.jpg", b"fake-jpeg-1")
    await media.simulate_client_upload("demo-household", "blob://b.jpg", b"fake-jpeg-2")
    service = AzureOpenAIVisionService(settings, media_storage=media, mock_fallback=False)

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": (
                        '{"is_same_item": true, "similarity_score": 0.95, "confidence": 0.90, '
                        '"reasoning": "Identical.", "visual_differences": "none"}'
                    )
                }
            }
        ]
    }

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=fake_resp):
        res = await service.compare_images("demo-household", "blob://a.jpg", "blob://b.jpg")
        assert res.is_same_item is True
        assert res.visual_differences == []


@pytest.mark.anyio
async def test_azure_vision_compare_images_null_visual_differences_defense():
    """驗證模型回傳 null (None) 視覺差異時，成功轉為空清單且不拋出 TypeError。"""
    from unittest.mock import AsyncMock, MagicMock, patch

    settings = VisionOpenAISettings(
        endpoint="https://fake.openai.azure.com/",
        api_key="valid-test-key",
        deployment="gpt-4o",
    )
    media = LocalStagingMediaStorage()
    await media.simulate_client_upload("demo-household", "blob://a.jpg", b"fake-jpeg-1")
    await media.simulate_client_upload("demo-household", "blob://b.jpg", b"fake-jpeg-2")
    service = AzureOpenAIVisionService(settings, media_storage=media, mock_fallback=False)

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": (
                        '{"is_same_item": true, "similarity_score": 0.92, "confidence": 0.88, '
                        '"reasoning": "Identical.", "visual_differences": null}'
                    )
                }
            }
        ]
    }

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=fake_resp):
        res = await service.compare_images("demo-household", "blob://a.jpg", "blob://b.jpg")
        assert res.is_same_item is True
        assert res.visual_differences == []

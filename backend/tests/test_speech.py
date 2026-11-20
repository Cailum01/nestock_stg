import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import status
from fastapi.testclient import TestClient
from identity_test_support import authenticated_client

from app.agent.llm import VertexAISettings
from app.core.dependencies import (
    _reset_speech_service_for_test,
    get_speech_service,
)
from app.main import app
from app.models.speech import AllowedAudioContentType, SpeechTranscribeResponse
from app.services.speech import (
    AzureSpeechService,
    MockSpeechToTextService,
    SpeechAzureSettings,
    SpeechToTextService,
    SpeechUpstreamError,
    VertexSpeechService,
    create_speech_service,
    load_speech_azure_settings,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "speech"


def test_mock_speech_service_protocol_conformance():
    service = MockSpeechToTextService()
    assert isinstance(service, SpeechToTextService)


@pytest.mark.anyio
async def test_mock_speech_service_transcribe_success():
    service = MockSpeechToTextService()
    fake_audio = b"\x00\x00\x00\x20ftypM4A " + b"x" * 100
    res = await service.transcribe(
        audio_bytes=fake_audio,
        content_type=AllowedAudioContentType.AUDIO_M4A,
        language="zh-TW",
    )
    assert isinstance(res, SpeechTranscribeResponse)
    assert res.transcript == "幫我查一下衛生紙"
    assert res.detected_language == "zh-TW"
    assert res.duration_seconds is not None
    assert res.duration_seconds > 0


@pytest.mark.anyio
async def test_mock_speech_service_transcribe_auto_language_fallback():
    service = MockSpeechToTextService()
    fake_audio = b"\x00\x00\x00\x20ftypM4A " + b"x" * 100
    res = await service.transcribe(
        audio_bytes=fake_audio,
        content_type=AllowedAudioContentType.AUDIO_M4A,
        language=None,
    )
    assert res.detected_language == "zh-TW"
    assert res.transcript == "幫我查一下衛生紙"


@pytest.mark.anyio
async def test_mock_speech_service_transcribe_explicit_language():
    service = MockSpeechToTextService()
    fake_audio = b"\x00\x00\x00\x20ftypM4A " + b"x" * 100
    res = await service.transcribe(
        audio_bytes=fake_audio,
        content_type=AllowedAudioContentType.AUDIO_M4A,
        language="en-US",
    )
    assert res.detected_language == "en-US"


@pytest.mark.anyio
async def test_mock_speech_service_silence_or_tiny_audio():
    service = MockSpeechToTextService()
    tiny_audio = b"\x00" * 10  # Less than 64 bytes
    res = await service.transcribe(
        audio_bytes=tiny_audio,
        content_type=AllowedAudioContentType.AUDIO_M4A,
    )
    assert res.transcript == ""
    assert res.duration_seconds == 0.0


@pytest.mark.anyio
async def test_azure_speech_service_fallback_without_keys(monkeypatch):
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)
    azure_service = AzureSpeechService(speech_key=None, service_region=None)
    assert isinstance(azure_service, SpeechToTextService)

    fake_audio = b"\x00\x00\x00\x20ftypM4A " + b"x" * 100
    res = await azure_service.transcribe(
        audio_bytes=fake_audio,
        content_type="audio/m4a",
        language=None,
    )
    assert isinstance(res, SpeechTranscribeResponse)
    assert res.detected_language == "zh-TW"


# ==============================================================================
# 金鑰加載隔離性與工廠依賴注入
# ==============================================================================


def test_load_speech_azure_settings_does_not_mutate_environ(monkeypatch):
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)

    isolated_env = {
        "AZURE_SPEECH_KEY": "test-key-12345",
        "AZURE_SPEECH_REGION": "japaneast",
    }
    settings = load_speech_azure_settings(environment=isolated_env)
    assert settings is not None
    assert settings.speech_key == "test-key-12345"
    assert settings.service_region == "japaneast"

    # Verify os.environ was not mutated by the isolated read
    assert "AZURE_SPEECH_KEY" not in os.environ


def test_load_speech_azure_settings_with_custom_endpoint():
    env = {
        "AZURE_SPEECH_KEY": "test-key-foundry",
        "AZURE_SPEECH_REGION": "eastasia",
        "AZURE_SPEECH_ENDPOINT": "https://foundrytest20260911.cognitiveservices.azure.com/",
    }
    settings = load_speech_azure_settings(environment=env)
    assert settings is not None
    assert settings.speech_key == "test-key-foundry"
    assert settings.service_region == "eastasia"
    assert settings.endpoint == "https://foundrytest20260911.cognitiveservices.azure.com"


def test_load_speech_azure_settings_decoupled_from_a1_openai_credentials():
    # 當僅設定 A1 的 AZURE_OPENAI_* 時，Speech 不會錯誤繼承，而是維持 None 啟動 Mock 模式
    env = {
        "AZURE_OPENAI_API_KEY": "a1-shared-key-999",
        "AZURE_OPENAI_ENDPOINT": "https://foundrytest20260911.services.ai.azure.com/openai/v1/responses",
        "AZURE_OPENAI_DEPLOYMENT": "gpt-4o",
    }
    assert load_speech_azure_settings(environment=env) is None


def test_load_speech_azure_settings_returns_none_when_empty():
    assert load_speech_azure_settings(environment={}) is None
    assert load_speech_azure_settings(environment={"AZURE_SPEECH_KEY": "   "}) is None


def test_create_speech_service_factory():
    # Without key and without mock flag -> None
    svc_unconfigured = create_speech_service(environment={})
    assert svc_unconfigured is None

    # Without key, but with explicit NESTOCK_SPEECH_MOCK=1 -> MockSpeechToTextService
    svc_mock = create_speech_service(environment={"NESTOCK_SPEECH_MOCK": "1"})
    assert isinstance(svc_mock, MockSpeechToTextService)

    # Legacy Azure credentials alone do not enable speech; Vertex references or explicit mock do.
    svc_azure = create_speech_service(environment={"AZURE_SPEECH_KEY": "secret-key"})
    assert svc_azure is None


@pytest.mark.anyio
async def test_vertex_speech_factory_uses_injected_client_and_preserves_response_contract():
    generate_content = AsyncMock(
        return_value=SimpleNamespace(
            text='{"transcript": "  幫我查衛生紙  ", "detected_language": "zh-TW"}'
        )
    )
    client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    )
    service = create_speech_service(
        environment={
            "NESTOCK_VERTEX_PROJECT_ID": "test-project",
            "NESTOCK_VERTEX_MODEL": "gemini-3.8-flash",
        },
        client=client,
    )

    assert isinstance(service, VertexSpeechService)
    assert service.settings == VertexAISettings(project="test-project", model="gemini-3.8-flash")
    response = await service.transcribe(b"mocked audio", "audio/wav", language="zh-TW")

    assert response.model_dump() == {
        "transcript": "幫我查衛生紙",
        "detected_language": "zh-TW",
        "duration_seconds": None,
    }
    assert generate_content.await_count == 1
    assert generate_content.await_args.kwargs["model"] == "gemini-3.8-flash"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("transcript", "detected_language", "expected_transcript"),
    [
        ("请帮我查询卫生纸", "zh-CN", "請幫我查詢衛生紙"),
        ("繁體中文的頭髮和幹活", "zh-CN", "繁體中文的頭髮和幹活"),
        ("Good morning, please help me", "en-US", "Good morning, please help me"),
    ],
)
async def test_vertex_speech_normalizes_transcript_and_preserves_detected_language(
    transcript: str,
    detected_language: str,
    expected_transcript: str,
):
    generate_content = AsyncMock(
        return_value=SimpleNamespace(
            text=json.dumps({"transcript": transcript, "detected_language": detected_language})
        )
    )
    client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    )
    service = VertexSpeechService(VertexAISettings(project="test-project"), client=client)

    response = await service.transcribe(b"mocked audio", "audio/wav")

    assert response.transcript == expected_transcript
    assert response.detected_language == detected_language


@pytest.mark.anyio
async def test_vertex_speech_maps_model_errors_without_provider_fallback():
    generate_content = AsyncMock(side_effect=RuntimeError("mock upstream error"))
    client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    )
    service = VertexSpeechService(VertexAISettings(project="test-project"), client=client)

    with pytest.raises(SpeechUpstreamError):
        await service.transcribe(b"mocked audio", "audio/wav")
    assert generate_content.await_count == 1


def test_dependencies_get_speech_service_lazy_singleton(monkeypatch, tmp_path):
    from app.services import speech

    # 隔離本機 backend/.env 避免污染測試
    monkeypatch.setattr(speech, "BACKEND_DIR", tmp_path)
    _reset_speech_service_for_test()
    try:
        # Default with no environment -> resolves None (unconfigured)
        monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
        monkeypatch.delenv("NESTOCK_SPEECH_MOCK", raising=False)
        monkeypatch.delenv("NESTOCK_VERTEX_PROJECT_ID", raising=False)
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        s1 = get_speech_service()
        s2 = get_speech_service()
        assert s1 is None
        assert s2 is None

        # Reset and provide NESTOCK_SPEECH_MOCK=1 -> resolves mock singleton
        _reset_speech_service_for_test()
        monkeypatch.setenv("NESTOCK_SPEECH_MOCK", "1")
        s_mock = get_speech_service()
        assert isinstance(s_mock, MockSpeechToTextService)

        # Reset and provide a project reference with a mocked Vertex client factory.
        _reset_speech_service_for_test()
        monkeypatch.delenv("NESTOCK_SPEECH_MOCK", raising=False)
        monkeypatch.setenv("NESTOCK_VERTEX_PROJECT_ID", "unit-test-project")
        monkeypatch.setattr(speech, "build_vertex_client", lambda settings: object())
        s_vertex = get_speech_service()
        assert isinstance(s_vertex, VertexSpeechService)
    finally:
        _reset_speech_service_for_test()
        monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
        monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)
        monkeypatch.delenv("NESTOCK_VERTEX_PROJECT_ID", raising=False)


# ==============================================================================
# 6 份語音測試夾具：不同模式在「無金鑰模式」下保證 100% 正常使用
# ==============================================================================

SIX_SPEECH_FIXTURES = [
    ("voice_zh_tw_query.wav", "audio/wav", "模式 1：真實繁中發聲 (幫我查一下衛生紙)"),
    ("voice_code_switching.wav", "audio/wav", "模式 2：真實中英混雜發聲 (買兩罐 Oatly 燕麥奶)"),
    ("voice_en_brand.wav", "audio/wav", "模式 3：真實英文品牌發聲 (Buy two Kirkland paper towels)"),
    (
        "voice_multi_items.wav",
        "audio/wav",
        "模式 4：多品項連續發聲 (鮮奶、雞蛋、醬油)",
    ),
    ("voice_standard_pcm.wav", "audio/wav", "模式 5：真實標準 PCM WAV 發聲 (庫存還有多少可口可樂)"),
    ("voice_silence_short.m4a", "audio/m4a", "模式 6：極短靜音/邊界防禦音訊 (<64 bytes)"),
]


@pytest.mark.anyio
@pytest.mark.parametrize("filename,content_type,desc", SIX_SPEECH_FIXTURES)
async def test_offline_mode_with_all_six_audio_fixtures(filename, content_type, desc):
    """驗證在無金鑰模式下，所有 6 種語音模式夾具皆能正常使用，不中斷、不崩潰。"""
    fixture_path = FIXTURES_DIR / filename
    assert fixture_path.exists(), f"測試音訊檔案缺失: {fixture_path}"

    audio_bytes = fixture_path.read_bytes()
    service = MockSpeechToTextService()

    res = await service.transcribe(
        audio_bytes=audio_bytes,
        content_type=content_type,
    )
    assert isinstance(res, SpeechTranscribeResponse)

    if filename == "voice_silence_short.m4a":
        # 模式 6: 14 bytes 極短音訊，防禦邊界觸發回傳空文字
        assert res.transcript == ""
        assert res.duration_seconds == 0.0
    else:
        # 模式 1~5: 正常長度音訊，回傳確定性模擬文字且時長 > 0
        assert len(res.transcript) > 0
        assert res.duration_seconds is not None
        assert res.duration_seconds > 0.0


# ==============================================================================
# Azure AI Speech 真實連線模擬（HTTP Mock：200 OK、真實語言解析、動態 MIME、Fail-Fast 異常）
# ==============================================================================


@pytest.mark.anyio
async def test_azure_speech_service_http_200_multilingual_success():
    captured_requests = []

    def mock_transport_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(
            200,
            json={
                "durationInMilliseconds": 1850,
                "combinedPhrases": [
                    {
                        "text": "買兩罐 Oatly 燕麥奶",
                        "locale": "zh-TW",
                    }
                ],
            },
        )

    settings = SpeechAzureSettings(speech_key="valid-key", service_region="eastasia")
    async with httpx.AsyncClient(transport=httpx.MockTransport(mock_transport_handler)) as client:
        service = AzureSpeechService(settings=settings, client=client, mock_fallback=False)
        audio_bytes = (FIXTURES_DIR / "voice_code_switching.wav").read_bytes()

        res = await service.transcribe(
            audio_bytes=audio_bytes,
            content_type="audio/wav",
            language=None,  # 未指定時啟用繁中 + 英文候選
        )

        assert res.transcript == "買兩罐 Oatly 燕麥奶"
        assert res.detected_language == "zh-TW"
        assert res.duration_seconds == 1.85

    assert len(captured_requests) == 1
    req = captured_requests[0]
    assert req.headers["Ocp-Apim-Subscription-Key"] == "valid-key"
    assert "locales" in req.content.decode("latin1")
    assert '"locales": ["zh-TW"]' in req.content.decode("latin1")


@pytest.mark.anyio
async def test_azure_speech_service_detects_real_english_locale():
    # 驗證 Azure 模型判定為 en-US 時，detected_language 能反映真實語言
    # (遵循 Azure v3.2 phrases schema 提取 locale)
    def mock_transport_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "durationMilliseconds": 1400,
                "combinedPhrases": [
                    {
                        "text": "Kirkland paper towel",
                    }
                ],
                "phrases": [
                    {
                        "text": "Kirkland paper towel",
                        "locale": "en-US",
                        "confidence": 0.88,
                    }
                ],
            },
        )

    settings = SpeechAzureSettings(speech_key="valid-key", service_region="eastasia")
    async with httpx.AsyncClient(transport=httpx.MockTransport(mock_transport_handler)) as client:
        service = AzureSpeechService(settings=settings, client=client, mock_fallback=False)
        audio_bytes = (FIXTURES_DIR / "voice_en_brand.wav").read_bytes()

        res = await service.transcribe(
            audio_bytes=audio_bytes,
            content_type="audio/wav",
            language=None,
        )

        assert res.transcript == "Kirkland paper towel"
        assert res.detected_language == "en-US"
        assert res.duration_seconds == 1.40


@pytest.mark.anyio
async def test_azure_speech_service_dynamic_mime_and_extension_mapping():
    captured_requests = []

    def mock_transport_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(
            200,
            json={"combinedPhrases": [{"text": "測試", "locale": "zh-TW"}]},
        )

    settings = SpeechAzureSettings(speech_key="valid-key", service_region="eastasia")
    async with httpx.AsyncClient(transport=httpx.MockTransport(mock_transport_handler)) as client:
        service = AzureSpeechService(settings=settings, client=client, mock_fallback=False)

        # 1. MP3
        await service.transcribe(audio_bytes=b"fake-mp3", content_type="audio/mpeg")
        # 2. AAC
        await service.transcribe(audio_bytes=b"fake-aac", content_type="audio/aac")
        # 3. M4A
        await service.transcribe(audio_bytes=b"fake-m4a", content_type="audio/m4a")

    assert len(captured_requests) == 3
    # Check filename extensions in multipart bodies
    assert 'filename="recording.mp3"' in captured_requests[0].content.decode("latin1")
    assert 'filename="recording.aac"' in captured_requests[1].content.decode("latin1")
    assert 'filename="recording.m4a"' in captured_requests[2].content.decode("latin1")


@pytest.mark.anyio
async def test_azure_speech_service_fail_fast_on_upstream_401():
    def mock_401_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": "401", "message": "Unauthorized"}})

    settings = SpeechAzureSettings(speech_key="invalid-key", service_region="eastasia")
    async with httpx.AsyncClient(transport=httpx.MockTransport(mock_401_handler)) as client:
        service = AzureSpeechService(settings=settings, client=client, mock_fallback=False)
        audio_bytes = b"fake-audio-bytes" * 50

        with pytest.raises(SpeechUpstreamError) as exc_info:
            await service.transcribe(
                audio_bytes=audio_bytes,
                content_type="audio/m4a",
            )

        assert "401" in str(exc_info.value)


@pytest.mark.anyio
async def test_azure_speech_service_fail_fast_on_network_failure():
    def mock_network_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("Connection timed out")

    settings = SpeechAzureSettings(speech_key="valid-key", service_region="eastasia")
    async with httpx.AsyncClient(transport=httpx.MockTransport(mock_network_error)) as client:
        service = AzureSpeechService(settings=settings, client=client, mock_fallback=False)
        audio_bytes = b"fake-audio-bytes" * 50

        with pytest.raises(SpeechUpstreamError) as exc_info:
            await service.transcribe(
                audio_bytes=audio_bytes,
                content_type="audio/wav",
            )

        assert "network failure" in str(exc_info.value).lower()


@pytest.mark.anyio
async def test_azure_speech_service_swagger_ui_placeholder_string_fallback():
    captured_requests = []

    def mock_transport_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(
            200,
            json={
                "durationMilliseconds": 1850,
                "combinedPhrases": [{"text": "測試", "locale": "zh-TW"}],
            },
        )

    settings = SpeechAzureSettings(speech_key="valid-key", service_region="eastasia")
    async with httpx.AsyncClient(transport=httpx.MockTransport(mock_transport_handler)) as client:
        service = AzureSpeechService(settings=settings, client=client, mock_fallback=False)
        audio_bytes = (FIXTURES_DIR / "voice_zh_tw_query.wav").read_bytes()

        # Swagger UI 常在 language 欄位預填 "string"，防禦邏輯應將其過濾回退為 ["zh-TW", "en-US"]
        res = await service.transcribe(
            audio_bytes=audio_bytes,
            content_type="audio/wav",
            language="string",
        )

        assert res.transcript == "測試"
        assert res.duration_seconds == 1.85

    assert len(captured_requests) == 1
    raw = captured_requests[0].content.decode("latin1")
    assert '"locales": ["zh-TW"]' in raw


@pytest.mark.anyio
async def test_azure_speech_service_custom_endpoint_routing():
    captured_requests = []

    def mock_transport_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(
            200,
            json={
                "durationInMilliseconds": 1200,
                "combinedPhrases": [{"text": "Foundry 端點測試成功", "locale": "zh-TW"}],
            },
        )

    settings = SpeechAzureSettings(
        speech_key="foundry-key",
        service_region="eastasia",
        endpoint="https://foundrytest20260911.cognitiveservices.azure.com/",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(mock_transport_handler)) as client:
        service = AzureSpeechService(settings=settings, client=client, mock_fallback=False)
        audio_bytes = (FIXTURES_DIR / "voice_zh_tw_query.wav").read_bytes()

        res = await service.transcribe(
            audio_bytes=audio_bytes,
            content_type="audio/wav",
        )

        assert res.transcript == "Foundry 端點測試成功"

    assert len(captured_requests) == 1
    req = captured_requests[0]
    assert req.url.host == "foundrytest20260911.cognitiveservices.azure.com"
    assert req.url.path == "/speechtotext/transcriptions:transcribe"
    assert req.url.query.decode("ascii") == "api-version=2024-11-15"
    assert req.headers["Ocp-Apim-Subscription-Key"] == "foundry-key"


def test_api_route_transcribe_speech_returns_502_on_upstream_failure(monkeypatch):
    """驗證當上游語音服務失敗拋出 SpeechServiceError 時，API 端點正確回傳 HTTP 502。"""

    class FaultySpeechService:
        async def transcribe(self, audio_bytes, content_type, language=None):
            raise SpeechUpstreamError("Azure 401 Unauthorized simulated: raw sensitive payload")

    app.dependency_overrides[get_speech_service] = lambda: FaultySpeechService()
    try:
        client = authenticated_client(app)
        fake_audio = b"\x00\x00\x00\x20ftypM4A " + b"x" * 100
        response = client.post(
            "/api/speech/transcribe",
            files={"file": ("recording.m4a", fake_audio, "audio/m4a")},
        )
        assert response.status_code == status.HTTP_502_BAD_GATEWAY
        detail = response.json()["detail"]
        assert "Speech transcription failed due to upstream service error" in detail
        # 確保 Azure 內部 raw payload 不會洩漏給用戶端
        assert "raw sensitive payload" not in detail
    finally:
        app.dependency_overrides.clear()


def test_api_route_transcribe_speech_returns_503_when_unconfigured():
    """驗證未配置語音服務 (None) 時，API 端點正確回傳 HTTP 503 Service Unavailable。"""
    app.dependency_overrides[get_speech_service] = lambda: None
    try:
        client = authenticated_client(app)
        fake_audio = b"\x00\x00\x00\x20ftypM4A " + b"x" * 100
        response = client.post(
            "/api/speech/transcribe",
            files={"file": ("recording.m4a", fake_audio, "audio/m4a")},
        )
        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        detail = response.json()["detail"]
        assert "Speech service is not configured" in detail
        assert "NESTOCK_VERTEX_PROJECT_ID" in detail
        assert "AZURE_SPEECH_KEY" not in detail
    finally:
        app.dependency_overrides.clear()


def test_api_route_transcribe_speech_requires_install_authentication():
    """Requests without valid install credentials must be rejected."""
    app.dependency_overrides.clear()
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/speech/transcribe",
                files={"file": ("recording.m4a", b"audio", "audio/m4a")},
            )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
    finally:
        app.dependency_overrides.clear()


def test_speech_service_unconfigured_without_injected_provider_settings(monkeypatch):
    """Unconfigured speech remains disabled without reading local files or credentials."""
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)
    monkeypatch.delenv("NESTOCK_SPEECH_MOCK", raising=False)
    monkeypatch.delenv("NESTOCK_VERTEX_PROJECT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    _reset_speech_service_for_test()
    try:
        service = get_speech_service()
        assert service is None
    finally:
        _reset_speech_service_for_test()


@pytest.mark.anyio
async def test_azure_speech_service_422_invalid_audio_format_raises_error():
    """驗證 Azure 回傳 422 InvalidAudioFormat 時拋出明確異常，不掩蓋為成功空字串。"""

    def mock_422_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "code": "UnprocessableEntity",
                "message": "The audio format is not supported.",
                "innerError": {
                    "code": "InvalidAudioFormat",
                    "message": "The audio format is not supported.",
                },
            },
        )

    settings = SpeechAzureSettings(speech_key="valid-key", service_region="eastasia")
    async with httpx.AsyncClient(transport=httpx.MockTransport(mock_422_handler)) as client:
        service = AzureSpeechService(settings=settings, client=client, mock_fallback=False)
        audio_bytes = b"fake-audio"

        with pytest.raises(SpeechUpstreamError) as exc_info:
            await service.transcribe(
                audio_bytes=audio_bytes,
                content_type="audio/wav",
            )
        assert "InvalidAudioFormat" in str(exc_info.value)


def test_openapi_schema_speech_dependency_has_no_environment_field():
    """驗證 FastAPI 依賴簽名已淨化，OpenAPI / Swagger 規格中絕不包含 environment 欄位。"""
    schema = app.openapi()
    body_schema = (
        schema.get("components", {})
        .get("schemas", {})
        .get("Body_transcribe_speech_api_speech_transcribe_post", {})
    )
    properties = body_schema.get("properties", {})
    assert "environment" not in properties
    assert "file" in properties
    assert "language" in properties

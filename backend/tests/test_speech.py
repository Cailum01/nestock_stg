import os
from pathlib import Path

import httpx
import pytest
from fastapi import status
from fastapi.testclient import TestClient

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
async def test_azure_speech_service_fallback_without_keys():
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
    # Without key -> MockSpeechToTextService
    svc_mock = create_speech_service(environment={})
    assert isinstance(svc_mock, MockSpeechToTextService)

    # With key -> AzureSpeechService
    svc_azure = create_speech_service(environment={"AZURE_SPEECH_KEY": "secret-key"})
    assert isinstance(svc_azure, AzureSpeechService)
    assert svc_azure.speech_key == "secret-key"
    assert svc_azure.endpoint is None

    # With key and custom endpoint
    svc_custom = create_speech_service(
        environment={
            "AZURE_SPEECH_KEY": "secret-key",
            "AZURE_SPEECH_ENDPOINT": "https://custom.endpoint.azure.com",
        }
    )
    assert isinstance(svc_custom, AzureSpeechService)
    assert svc_custom.endpoint == "https://custom.endpoint.azure.com"


def test_dependencies_get_speech_service_lazy_singleton(monkeypatch):
    _reset_speech_service_for_test()
    try:
        # Default with no environment -> resolves mock singleton
        monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
        s1 = get_speech_service()
        s2 = get_speech_service()
        assert isinstance(s1, MockSpeechToTextService)
        assert isinstance(s2, MockSpeechToTextService)
        assert s1 is s2

        # Reset and provide live key
        _reset_speech_service_for_test()
        monkeypatch.setenv("AZURE_SPEECH_KEY", "key-xyz")
        s_live = get_speech_service()
        assert isinstance(s_live, AzureSpeechService)
    finally:
        _reset_speech_service_for_test()


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
    assert "zh-TW" in req.content.decode("latin1")
    assert "en-US" in req.content.decode("latin1")


@pytest.mark.anyio
async def test_azure_speech_service_detects_real_english_locale():
    # 驗證 Azure 模型判定為 en-US 時，領域模型 detected_language 能正確反映真實語言
    def mock_transport_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "durationInMilliseconds": 1400,
                "combinedPhrases": [
                    {
                        "text": "Kirkland paper towel",
                        "locale": "en-US",
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
    assert '"locales": ["zh-TW", "en-US"]' in raw


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
            raise SpeechUpstreamError("Azure 401 Unauthorized simulated")

    app.dependency_overrides[get_speech_service] = lambda: FaultySpeechService()
    try:
        client = TestClient(app)
        fake_audio = b"\x00\x00\x00\x20ftypM4A " + b"x" * 100
        response = client.post(
            "/api/speech/transcribe",
            files={"file": ("recording.m4a", fake_audio, "audio/m4a")},
        )
        assert response.status_code == status.HTTP_502_BAD_GATEWAY
        assert "Speech transcription failed" in response.json()["detail"]
    finally:
        app.dependency_overrides.clear()

import pytest

from app.models.speech import AllowedAudioContentType, SpeechTranscribeResponse
from app.services.speech import (
    AzureSpeechService,
    MockSpeechToTextService,
    SpeechToTextService,
)


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

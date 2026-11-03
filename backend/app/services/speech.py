import os
from typing import Protocol, runtime_checkable

from app.models.speech import SpeechTranscribeResponse


@runtime_checkable
class SpeechToTextService(Protocol):
    """Port for multimodal speech-to-text transcription service."""

    async def transcribe(
        self,
        audio_bytes: bytes,
        content_type: str,
        language: str | None = None,
    ) -> SpeechTranscribeResponse: ...


class MockSpeechToTextService:
    """Mock speech-to-text provider for local development, CI/CD, and deterministic testing."""

    async def transcribe(
        self,
        audio_bytes: bytes,
        content_type: str,
        language: str | None = None,
    ) -> SpeechTranscribeResponse:
        effective_language = language or "zh-TW"

        # Silence, tiny audio, or explicit test failure returns empty transcript
        if len(audio_bytes) < 64 or b"FAIL" in audio_bytes or b"ERROR" in audio_bytes:
            return SpeechTranscribeResponse(
                transcript="",
                detected_language=effective_language,
                duration_seconds=0.0,
            )

        duration = round(max(0.5, len(audio_bytes) / 32000.0), 2)
        return SpeechTranscribeResponse(
            transcript="幫我查一下衛生紙",
            detected_language=effective_language,
            duration_seconds=duration,
        )


class AzureSpeechService:
    """Azure AI Speech Fast Transcription (REST API v3.2) STT adapter with graceful degradation."""

    def __init__(
        self,
        speech_key: str | None = None,
        service_region: str | None = None,
        mock_fallback: bool = True,
    ) -> None:
        self.speech_key = speech_key or os.environ.get("AZURE_SPEECH_KEY")
        self.service_region = service_region or os.environ.get("AZURE_SPEECH_REGION", "eastasia")
        self.mock_fallback = mock_fallback
        self._fallback_service = MockSpeechToTextService()

    async def transcribe(
        self,
        audio_bytes: bytes,
        content_type: str,
        language: str | None = None,
    ) -> SpeechTranscribeResponse:
        # Offline mode: If no credentials are configured and fallback is enabled, use mock
        if not self.speech_key:
            if self.mock_fallback:
                return await self._fallback_service.transcribe(audio_bytes, content_type, language)
            raise ValueError("AZURE_SPEECH_KEY is required but not configured.")

        effective_language = language or "zh-TW"

        # Azure AI Speech Fast Transcription API v3.2 (Natively supports M4A, AAC, WAV, MP3)
        url = (
            f"https://{self.service_region}.api.cognitive.microsoft.com"
            f"/speechtotext/transcriptions:transcribe?api-version=2024-11-15"
        )
        headers = {
            "Ocp-Apim-Subscription-Key": self.speech_key,
            "Accept": "application/json",
        }

        try:
            import json

            import httpx

            ext = "m4a" if "m4a" in (content_type or "") else "wav"
            files = {
                "audio": (f"recording.{ext}", audio_bytes, content_type or "audio/m4a"),
                "definition": (
                    None,
                    json.dumps({"locales": [effective_language]}),
                    "application/json",
                ),
            }

            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(url, headers=headers, files=files)
                if res.status_code == 200:
                    data = res.json()
                    phrases = data.get("combinedPhrases", [])
                    transcript = phrases[0].get("text", "") if phrases else ""
                    duration_ms = data.get("durationInMilliseconds", len(audio_bytes) / 32)
                    duration_s = round(max(0.1, duration_ms / 1000.0), 2)
                    return SpeechTranscribeResponse(
                        transcript=transcript,
                        detected_language=effective_language,
                        duration_seconds=duration_s,
                    )

                # Recognition failure or upstream error: do not disguise with mock text
                return SpeechTranscribeResponse(
                    transcript="",
                    detected_language=effective_language,
                    duration_seconds=0.0,
                )
        except Exception:
            # On network/parsing failure when real key is provided, return empty transcript
            return SpeechTranscribeResponse(
                transcript="",
                detected_language=effective_language,
                duration_seconds=0.0,
            )

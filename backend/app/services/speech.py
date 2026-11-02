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

        if len(audio_bytes) < 64:
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
    """Azure AI Speech (Cognitive Services) STT adapter with graceful fallback."""

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
        # Tier 3: Safe offline degradation if no credentials are configured
        if not self.speech_key:
            if self.mock_fallback:
                return await self._fallback_service.transcribe(audio_bytes, content_type, language)
            raise ValueError("AZURE_SPEECH_KEY is required but not configured.")

        effective_language = language or "zh-TW"
        url = (
            f"https://{self.service_region}.stt.speech.microsoft.com"
            f"/speech/recognition/conversation/cognitiveservices/v1?language={effective_language}"
        )
        headers = {
            "Ocp-Apim-Subscription-Key": self.speech_key,
            "Content-Type": content_type or "audio/m4a",
            "Accept": "application/json",
        }

        try:
            import httpx

            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(url, headers=headers, content=audio_bytes)
                if res.status_code == 200:
                    data = res.json()
                    transcript = data.get("DisplayText") or data.get("RecognitionStatus", "")
                    return SpeechTranscribeResponse(
                        transcript=transcript,
                        detected_language=effective_language,
                        duration_seconds=round(max(0.5, len(audio_bytes) / 32000.0), 2),
                    )
                # Tier 2: Internal degradation on non-200 responses
                if self.mock_fallback:
                    return await self._fallback_service.transcribe(
                        audio_bytes, content_type, language
                    )
                res.raise_for_status()
        except Exception:
            if self.mock_fallback:
                return await self._fallback_service.transcribe(audio_bytes, content_type, language)
            raise

        return await self._fallback_service.transcribe(audio_bytes, content_type, language)

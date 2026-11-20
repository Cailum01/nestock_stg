import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

import httpx
from opencc import OpenCC

from app.agent.llm import VertexAISettings, build_vertex_client, load_vertex_settings
from app.models.speech import SpeechTranscribeResponse

logger = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parents[2]

MIME_TO_EXT: dict[str, str] = {
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/m4a": "m4a",
    "audio/x-m4a": "m4a",
    "audio/aac": "aac",
    "audio/mp4": "m4a",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/webm": "webm",
}

_ZH_TW_CONVERTER = OpenCC("s2tw")
_ZH_TW_TO_SIMPLIFIED_CONVERTER = OpenCC("tw2s")


def _normalize_zh_tw_transcript(transcript: str) -> str:
    """Convert Simplified Chinese to Taiwan-variant Traditional without changing other text."""
    if _ZH_TW_TO_SIMPLIFIED_CONVERTER.convert(transcript) != transcript:
        return transcript
    return _ZH_TW_CONVERTER.convert(transcript)


class SpeechServiceError(Exception):
    """Base exception for speech-to-text service domain errors."""


class SpeechUpstreamError(SpeechServiceError):
    """Raised when an upstream speech model fails or returns an invalid response."""


class VertexSpeechService:
    """Gemini audio transcription over Vertex AI using ADC."""

    def __init__(
        self,
        settings: VertexAISettings,
        client: Any | None = None,
    ) -> None:
        self.settings = settings
        self._client = client if client is not None else build_vertex_client(settings)

    async def transcribe(
        self,
        audio_bytes: bytes,
        content_type: str,
        language: str | None = None,
    ) -> SpeechTranscribeResponse:
        from google.genai import types

        clean_language = (language or "").strip()
        if clean_language.lower() in ("string", "null", "undefined"):
            clean_language = ""
        language_hint = clean_language or "detect the spoken language"
        prompt = (
            f"Transcribe this audio faithfully. Language guidance: {language_hint}. "
            'Return JSON only with keys "transcript" and "detected_language". '
            "Use a BCP-47 language code for detected_language."
        )
        mime = (content_type or "audio/wav").split(";")[0].strip().lower()
        try:
            response = await self._client.aio.models.generate_content(
                model=self.settings.model,
                contents=[
                    prompt,
                    types.Part.from_bytes(data=audio_bytes, mime_type=mime),
                ],
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )
            text = getattr(response, "text", None)
            if not isinstance(text, str) or not text.strip():
                raise ValueError("Vertex speech returned an invalid response.")
            result = json.loads(text)
            transcript = result.get("transcript")
            detected_language = result.get("detected_language")
            if not isinstance(transcript, str):
                raise ValueError("Vertex speech omitted the transcript.")
            if not isinstance(detected_language, str) or not detected_language.strip():
                detected_language = clean_language or "zh-TW"
        except Exception as exc:
            logger.error("Vertex speech transcription failed: %s", exc)
            raise SpeechUpstreamError("Vertex speech transcription failed.") from exc

        return SpeechTranscribeResponse(
            transcript=_normalize_zh_tw_transcript(transcript.strip()),
            detected_language=detected_language.strip(),
            duration_seconds=None,
        )


@dataclass(frozen=True)
class SpeechAzureSettings:
    """Dedicated Azure AI Speech configuration (A2)."""

    speech_key: str
    service_region: str = "eastasia"
    endpoint: str | None = None


def load_speech_azure_settings(
    environment: Mapping[str, str] | None = None,
) -> SpeechAzureSettings | None:
    """Load speech settings from environment or backend/.env without overriding process variables.

    Strictly decoupled: only activates when AZURE_SPEECH_KEY is provided.
    Does NOT fall back to AZURE_OPENAI_API_KEY, avoiding credential mismatches.
    """
    if environment is None:
        from dotenv import load_dotenv

        load_dotenv(BACKEND_DIR / ".env", override=False)
        env = os.environ
    else:
        env = environment

    key = env.get("AZURE_SPEECH_KEY", "").strip()
    if not key:
        return None

    region = env.get("AZURE_SPEECH_REGION", "").strip() or "eastasia"

    raw_endpoint = env.get("AZURE_SPEECH_ENDPOINT", "").strip()
    endpoint: str | None = None
    if raw_endpoint:
        parts = urlsplit(raw_endpoint if "://" in raw_endpoint else f"https://{raw_endpoint}")
        netloc = parts.netloc
        if "services.ai.azure.com" in netloc:
            subdomain = netloc.split(".")[0]
            endpoint = f"https://{subdomain}.cognitiveservices.azure.com"
        else:
            endpoint = f"{parts.scheme}://{netloc}"

    return SpeechAzureSettings(
        speech_key=key,
        service_region=region,
        endpoint=endpoint,
    )


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
    """Azure AI Speech Fast Transcription (REST API) STT adapter."""

    def __init__(
        self,
        settings: SpeechAzureSettings | None = None,
        speech_key: str | None = None,
        service_region: str | None = None,
        endpoint: str | None = None,
        mock_fallback: bool = True,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if settings is not None:
            self.speech_key = settings.speech_key
            self.service_region = settings.service_region
            self.endpoint = settings.endpoint
        else:
            self.speech_key = speech_key or os.environ.get("AZURE_SPEECH_KEY", "").strip() or None
            self.service_region = (
                service_region or os.environ.get("AZURE_SPEECH_REGION", "eastasia").strip()
            )
            raw_endpoint = endpoint or os.environ.get("AZURE_SPEECH_ENDPOINT", "").strip()
            if raw_endpoint:
                parts = urlsplit(
                    raw_endpoint if "://" in raw_endpoint else f"https://{raw_endpoint}"
                )
                netloc = parts.netloc
                if "services.ai.azure.com" in netloc:
                    subdomain = netloc.split(".")[0]
                    self.endpoint = f"https://{subdomain}.cognitiveservices.azure.com"
                else:
                    self.endpoint = f"{parts.scheme}://{netloc}"
            else:
                self.endpoint = None

        self.mock_fallback = mock_fallback
        self._external_client = client is not None
        self._client = client or httpx.AsyncClient(timeout=25.0)
        self._fallback_service = MockSpeechToTextService()

    async def aclose(self) -> None:
        """Close managed HTTP client if internally created."""
        if not self._external_client and self._client is not None:
            await self._client.aclose()

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

        clean_language = (language or "").strip()
        if not clean_language or clean_language.lower() in ("string", "null", "undefined"):
            clean_language = None

        # Single Traditional Chinese candidate strategy: Azure's zh-TW acoustic model
        # natively recognizes code-switching (English brand names/goods) and full English
        # queries while avoiding the 429 Resource Exhausted rate limit of multi-locale setups.
        locales = [clean_language] if clean_language else ["zh-TW"]

        # Azure AI Speech Fast Transcription API
        if self.endpoint:
            base_url = self.endpoint.rstrip("/")
            url = f"{base_url}/speechtotext/transcriptions:transcribe?api-version=2024-11-15"
        else:
            url = (
                f"https://{self.service_region}.api.cognitive.microsoft.com"
                f"/speechtotext/transcriptions:transcribe?api-version=2024-11-15"
            )
        headers = {
            "Ocp-Apim-Subscription-Key": self.speech_key,
            "Accept": "application/json",
        }

        # Dynamic MIME-to-extension mapping
        clean_mime = (content_type or "").split(";")[0].strip().lower()
        ext = MIME_TO_EXT.get(clean_mime, "wav")

        files = {
            "audio": (f"recording.{ext}", audio_bytes, clean_mime or "audio/wav"),
            "definition": (
                None,
                json.dumps({"locales": locales}),
                "application/json",
            ),
        }

        try:
            res = await self._client.post(url, headers=headers, files=files)
        except httpx.RequestError as exc:
            logger.error("Azure AI Speech network failure: %s", exc)
            raise SpeechUpstreamError(f"Azure AI Speech network failure: {exc}") from exc

        if res.status_code == 200:
            data = res.json()
            combined = data.get("combinedPhrases", [])
            transcript = combined[0].get("text", "") if combined else ""

            # Extract detected locale from detailed phrases (Azure Fast Transcription v3.2 schema),
            # falling back to top-level locale, specified language, or zh-TW
            detailed_phrases = data.get("phrases", [])
            azure_detected_locale = (
                detailed_phrases[0].get("locale")
                if detailed_phrases and isinstance(detailed_phrases[0], dict)
                else None
            )
            detected_locale = (
                azure_detected_locale
                or (combined[0].get("locale") if combined else None)
                or data.get("locale")
                or clean_language
                or "zh-TW"
            )

            duration_ms = data.get("durationMilliseconds") or data.get("durationInMilliseconds")
            if duration_ms is not None:
                duration_s = round(max(0.1, duration_ms / 1000.0), 2)
            else:
                duration_s = None

            return SpeechTranscribeResponse(
                transcript=transcript,
                detected_language=detected_locale,
                duration_seconds=duration_s,
            )

        if res.status_code == 422:
            logger.warning(
                "Azure AI Speech rejected audio payload as unprocessable (HTTP 422): %s",
                res.text,
            )
            try:
                err_data = res.json()
                inner_code = err_data.get("innerError", {}).get("code", "")
                if inner_code == "InvalidAudioFormat":
                    err_msg = err_data.get("message", "InvalidAudioFormat")
                    raise SpeechUpstreamError(
                        f"Unsupported or corrupted audio format ({inner_code}): {err_msg}"
                    )
            except (ValueError, TypeError):
                pass

            return SpeechTranscribeResponse(
                transcript="",
                detected_language=clean_language or "zh-TW",
                duration_seconds=0.0,
            )

        logger.error(
            "Azure AI Speech Fast Transcription returned HTTP %s: %s",
            res.status_code,
            res.text,
        )
        raise SpeechUpstreamError(
            f"Azure AI Speech upstream error (HTTP {res.status_code}): {res.text}"
        )


def create_speech_service(
    environment: Mapping[str, str] | None = None,
    client: Any | None = None,
) -> SpeechToTextService | None:
    """Create Vertex speech, an explicitly enabled mock, or None."""
    vertex_settings = load_vertex_settings(environment=environment)
    if vertex_settings is not None:
        return VertexSpeechService(settings=vertex_settings, client=client)

    env = os.environ if environment is None else environment
    mock_flag = env.get("NESTOCK_SPEECH_MOCK", "").strip().lower()
    if mock_flag in ("1", "true", "yes"):
        return MockSpeechToTextService()

    return None

"""Pytest End-to-End Speech Transcription and Exact Match Latency Test Suite."""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest
from dotenv import load_dotenv

from app.core.dependencies import _reset_speech_service_for_test, get_speech_service
from app.main import app
from app.models.speech import SpeechTranscribeResponse
from app.services.speech import BACKEND_DIR, load_speech_azure_settings
from tests.speech_e2e.speech_tts import (
    CASES_DIR,
    DEFAULT_VOICE,
    GENERATED_DIR,
    SUPPORTED_VOICES,
    ensure_audio_fixture,
    get_audio_upload_payload,
    load_batch_cases,
    normalize_for_exact_match,
)

E2E_DIR = Path(__file__).resolve().parent


def test_batch_json_parsing_and_validation():
    """Verify loading and validation of batch JSON case files including namespaces."""
    batch_file = CASES_DIR / "parse_voice_1.json"
    cases = load_batch_cases(batch_file)
    assert len(cases) >= 4
    assert cases[0]["id"] == "TC_SPEECH_001"
    assert cases[0]["text"] == "幫我查一下衛生紙"
    assert cases[0]["batch_name"] == "parse_voice_1"
    assert cases[3]["id"] == "TC_SPEECH_004"
    assert cases[3]["voice"] == SUPPORTED_VOICES["zh_mixed_female"]


def test_normalize_for_exact_match_helper():
    """Verify punctuation stripping and whitespace cleanup for 100% exact match comparison."""
    assert normalize_for_exact_match("幫我查一下衛生紙。") == "幫我查一下衛生紙"
    assert normalize_for_exact_match("，家裡還有鮮奶嗎！？") == "家裡還有鮮奶嗎"
    assert normalize_for_exact_match("  買2瓶可口可樂   ") == "買2瓶可口可樂"
    assert normalize_for_exact_match("") == ""


def _require_speech_e2e_deps() -> None:
    import importlib.util

    if not (importlib.util.find_spec("edge_tts") and importlib.util.find_spec("imageio_ffmpeg")):
        pytest.skip(
            "Optional dependencies 'edge-tts' or 'imageio-ffmpeg' missing. "
            "Install via: uv sync --group speech-e2e"
        )


@pytest.mark.speech_e2e
@pytest.mark.anyio
async def test_audio_fixture_caching_idempotency(tmp_path: Path):
    """Verify that synthesizing to genuine WAV twice hits cache and respects batch prefix."""
    _require_speech_e2e_deps()
    case_id = "TC_CACHE_TEST"
    text = "測試快取機制"
    batch_name = "parse_voice_1"

    # 1. First call: Generates genuine .wav file with batch namespace prefix
    path1 = await ensure_audio_fixture(case_id, text, output_dir=tmp_path, batch_name=batch_name)
    assert path1.exists()
    assert path1.name == "parse_voice_1_TC_CACHE_TEST.wav"
    assert path1.suffix == ".wav"
    assert path1.stat().st_size > 0
    # Verify genuine RIFF/WAVE header
    assert b"RIFF" in path1.read_bytes()[:4]
    assert b"WAVE" in path1.read_bytes()[:12]
    mtime1 = path1.stat().st_mtime_ns

    # 2. Second call: Cache hit (same mtime)
    path2 = await ensure_audio_fixture(case_id, text, output_dir=tmp_path, batch_name=batch_name)
    assert path1 == path2
    assert path2.stat().st_mtime_ns == mtime1


@pytest.mark.speech_e2e
@pytest.mark.anyio
async def test_speech_e2e_single_turn_with_latency_and_exact_match():
    """Single-turn E2E speech transcription with exact-match verification and latency timing."""
    _require_speech_e2e_deps()
    # Ensure test isolation: avoid overriding test runner environment variables
    load_dotenv(BACKEND_DIR / ".env", override=False)
    _reset_speech_service_for_test()

    batch_file = CASES_DIR / "parse_voice_1.json"
    cases = load_batch_cases(batch_file)

    # Check if live Azure Speech credentials are available
    settings = load_speech_azure_settings()
    is_live = settings is not None and bool(settings.speech_key)

    if not is_live:
        # In offline/mock mode, provide a mock service that echoes the text for exact match testing
        class EchoMockSpeechService:
            def __init__(self, current_text: str = ""):
                self.current_text = current_text

            async def transcribe(
                self,
                audio_bytes: bytes,
                content_type: str,
                language: str | None = None,
            ):
                duration = round(max(0.5, len(audio_bytes) / 32000.0), 2)
                return SpeechTranscribeResponse(
                    transcript=self.current_text,
                    detected_language=language or "zh-TW",
                    duration_seconds=duration,
                )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        timeout=30.0,
    ) as client:
        for case in cases:
            case_id = case["id"]
            expected_text = case["text"]

            # 1. Synthesize or retrieve cached audio fixture as standard WAV
            audio_path = await ensure_audio_fixture(
                case_id=case_id,
                text=expected_text,
                voice=case.get("voice", DEFAULT_VOICE),
                rate=case.get("rate", "+0%"),
                volume=case.get("volume", "+0%"),
                pitch=case.get("pitch", "+0Hz"),
                output_dir=GENERATED_DIR,
                batch_name=case.get("batch_name"),
            )
            assert audio_path.exists()
            assert audio_path.name == f"{case.get('batch_name')}_{case_id}.wav"
            assert audio_path.suffix == ".wav"
            filename, audio_bytes, content_type = get_audio_upload_payload(audio_path)
            assert filename == "recording.wav"
            assert content_type == "audio/wav"
            assert len(audio_bytes) > 0

            # Configure mock service if offline
            if not is_live:
                echo_service = EchoMockSpeechService(current_text=expected_text)
                app.dependency_overrides[get_speech_service] = lambda: echo_service

            try:
                # 2. High-resolution monotonic timing probe (T0)
                t0 = time.perf_counter()

                # 3. Single-turn HTTP POST to API endpoint simulating upload
                files = {
                    "file": (filename, audio_bytes, content_type),
                }
                response = await client.post("/api/speech/transcribe", files=files)

                # 4. Stop timer immediately after full response received (T1)
                t1 = time.perf_counter()
                latency_ms = (t1 - t0) * 1000.0

                # 5. Assert HTTP 200 response
                if response.status_code == 429 or (
                    response.status_code == 502 and "TooManyRequests" in response.text
                ):
                    pytest.skip(f"[{case_id}] Azure AI Speech API quota exceeded (HTTP 429)")

                assert response.status_code == 200, (
                    f"[{case_id}] Expected 200 OK, got {response.status_code}: {response.text}"
                )

                data = response.json()
                raw_transcript = data.get("transcript", "")
                norm_transcript = normalize_for_exact_match(raw_transcript)
                norm_expected = normalize_for_exact_match(expected_text)

                # 6. Assert 100% exact match
                assert norm_transcript == norm_expected, (
                    f"[{case_id}] Exact match failed! "
                    f"Expected '{norm_expected}', got '{norm_transcript}'"
                )

                # 7. Assert reasonable latency threshold (allow up to 10s for live WAN cloud calls)
                max_latency_threshold_ms = 10000.0 if is_live else 800.0
                assert latency_ms <= max_latency_threshold_ms, (
                    f"[{case_id}] Latency {latency_ms:.2f}ms exceeded {max_latency_threshold_ms}ms"
                )

            finally:
                if not is_live:
                    app.dependency_overrides.pop(get_speech_service, None)

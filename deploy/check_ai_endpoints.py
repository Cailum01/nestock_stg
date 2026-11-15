"""B2 check: Azure OpenAI, Vision and Speech answer on the CURRENT endpoints, and
`NESTOCK_SPEECH_MOCK` is not set (checklist §6 "OpenAI / Vision / Speech connectivity").

It reads the same settings the backend reads (process environment, then `backend/.env`),
so it tests exactly the endpoints the app would use. It makes real, tiny calls:
  - OpenAI  : one 16-token Responses API call on the Agent deployment
  - Vision  : one 16-token call with a 16x16 test image on the Vision deployment
              (falls back to the Agent endpoint / deployment, as backend/app/services/vision.py)
  - Speech  : one Fast Transcription call with deploy/smoke-audio-detergent.wav
It prints host names, deployment names and results. It never prints a key.

Scope: this proves the services answer from THIS machine with THESE settings. It cannot
prove the deployed runtime has the same settings; `deploy/smoke_test.py` (agent and speech
steps) does that against the cloud backend. Run both.

Any failure exits 1. Nothing is mocked or skipped silently.

Usage:
  cd backend
  uv run python ../deploy/check_ai_endpoints.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from dotenv import dotenv_values, load_dotenv  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
AUDIO = Path(__file__).resolve().parent / "smoke-audio-detergent.wav"
SPEECH_EXPECTED_WORD = "洗衣精"
TRUTHY = {"1", "true", "yes"}


def load_environment() -> None:
    """Same precedence as the backend: real environment wins over backend/.env."""
    load_dotenv(BACKEND_DIR / ".env", override=False)


def check_speech_mock_unset() -> str:
    from_env = os.environ.get("NESTOCK_SPEECH_MOCK")
    from_file = dotenv_values(BACKEND_DIR / ".env").get("NESTOCK_SPEECH_MOCK")
    # The backend reads any non-empty value as a decision to look at; only 1/true/yes enable
    # the mock, but a leftover variable of any value is a Demo-day accident waiting to happen.
    for source, value in (("environment", from_env), ("backend/.env", from_file)):
        if value is not None and value.strip() != "":
            enabled = value.strip().lower() in TRUTHY
            raise CheckFailure(
                f"NESTOCK_SPEECH_MOCK is set in {source}"
                + (" and ENABLES the mock" if enabled else " (value is not enabling, remove it)")
            )
    return "NESTOCK_SPEECH_MOCK is not set"


class CheckFailure(Exception):
    pass


def host(url: str) -> str:
    return urlsplit(url if "://" in url else f"https://{url}").netloc


def post_json(url: str, api_key: str, document: dict, timeout: float = 60.0) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(document).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "api-key": api_key},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise CheckFailure(f"HTTP {error.code} from {host(url)}") from error
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        raise CheckFailure(f"cannot reach {host(url)} ({type(error).__name__})") from error


def check_openai() -> str:
    from app.agent.llm import load_azure_settings

    settings = load_azure_settings()
    if settings is None:
        raise CheckFailure("AZURE_OPENAI_ENDPOINT / _API_KEY / _DEPLOYMENT are not all set")
    post_json(
        settings.base_url + "responses",
        settings.api_key,
        {"model": settings.deployment, "input": "ping", "max_output_tokens": 16},
    )
    return f"{host(settings.endpoint)} deployment={settings.deployment}"


def _test_png() -> str:
    """A 16x16 solid-colour PNG, built here so no binary file is needed."""
    width = height = 16
    raw = b"".join(b"\x00" + b"\x90\xa0\xb0" * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    return base64.b64encode(png).decode("ascii")


def check_vision() -> str:
    from app.services.vision import load_vision_azure_settings

    settings = load_vision_azure_settings()
    if settings is None:
        raise CheckFailure("no Vision endpoint / key (AZURE_OPENAI_VISION_* or AZURE_OPENAI_*)")
    post_json(
        settings.base_url + "responses",
        settings.api_key,
        {
            "model": settings.deployment,
            "max_output_tokens": 16,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Reply with one word: the colour."},
                        {
                            "type": "input_image",
                            "image_url": f"data:image/png;base64,{_test_png()}",
                        },
                    ],
                }
            ],
        },
    )
    return f"{host(settings.endpoint)} deployment={settings.deployment}"


def check_speech() -> str:
    from app.services.speech import AzureSpeechService, load_speech_azure_settings

    settings = load_speech_azure_settings()
    if settings is None:
        raise CheckFailure("AZURE_SPEECH_KEY is not set")
    try:
        audio = AUDIO.read_bytes()
    except OSError as error:
        raise CheckFailure(f"test audio unreadable: {AUDIO}") from error
    try:
        result = asyncio.run(
            AzureSpeechService(settings=settings, mock_fallback=False).transcribe(
                audio_bytes=audio, content_type="audio/wav", language="zh-TW"
            )
        )
    except Exception as error:  # noqa: BLE001 - any upstream failure is a failed check
        raise CheckFailure(f"Speech call failed ({type(error).__name__})") from error
    if SPEECH_EXPECTED_WORD not in result.transcript:
        raise CheckFailure(
            f"transcript {result.transcript!r} does not contain {SPEECH_EXPECTED_WORD!r}"
        )
    return f"region={settings.service_region} transcript={result.transcript.strip()!r}"


CHECKS = [
    ("NESTOCK_SPEECH_MOCK unset", check_speech_mock_unset),
    ("Azure OpenAI (Agent)", check_openai),
    ("Vision", check_vision),
    ("Speech (real transcription)", check_speech),
]


def main() -> int:
    load_environment()
    failed = False
    for label, check in CHECKS:
        started = time.perf_counter()
        try:
            detail = check()
        except CheckFailure as failure:
            print(f"FAIL  {label}: {failure}")
            failed = True
            continue
        print(f"PASS  {label} ({time.perf_counter() - started:.1f}s) {detail}")
    if failed:
        print(
            "\nAt least one AI endpoint check failed. Do not start the Demo; no mock replaces it."
        )
        return 1
    print("\nAll AI endpoint checks passed from this machine.")
    print("Cloud runtime still needs deploy/smoke_test.py (agent + speech steps).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

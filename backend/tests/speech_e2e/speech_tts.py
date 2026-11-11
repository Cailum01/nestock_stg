"""TTS synthesis, transcoding to lossless 16kHz PCM WAV, caching, and normalization."""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import subprocess
from pathlib import Path

import edge_tts
import imageio_ffmpeg

logger = logging.getLogger(__name__)

SUPPORTED_VOICES: dict[str, str] = {
    # 台灣繁體中文系列
    "zh_tw_female": "zh-TW-HsiaoChenNeural",  # 台灣繁中標準女聲 (預設)
    "zh_tw_male": "zh-TW-YunJheNeural",  # 台灣繁中成熟男聲 (僅支援預設聲學參數)
    "zh_tw_friendly": "zh-TW-HsiaoYuNeural",  # 台灣繁中親切女聲
    # 中英混雜 / 雙語品牌詞強化旗艦系列
    "zh_mixed_female": "zh-CN-XiaoxiaoNeural",  # 中英混雜女聲旗艦 (如 Oatly, Kirkland, iPhone)
    "zh_mixed_male": "zh-CN-YunxiNeural",  # 中英混雜男聲旗艦
}

# 微軟 ReadAloud 伺服器限制：YunJhe 男聲音色不支援 prosody 調整，強行調整會回傳 NoAudioReceived
VOICES_WITHOUT_PROSODY: frozenset[str] = frozenset({"zh-TW-YunJheNeural"})

DEFAULT_VOICE = SUPPORTED_VOICES["zh_tw_female"]
E2E_DIR = Path(__file__).resolve().parent
GENERATED_DIR = E2E_DIR / ".generated"
CASES_DIR = E2E_DIR / "cases"


def resolve_batch_path(batch_input: str | Path) -> Path:
    """Smartly resolve batch JSON path across multiple common relative candidate locations."""
    p = Path(batch_input)
    if p.is_file():
        return p.resolve()

    candidates = [
        CASES_DIR / p,
        E2E_DIR / p,
        CASES_DIR / f"{p.name}.json" if not p.suffix else None,
        CASES_DIR / p.name,
    ]
    for cand in candidates:
        if cand and cand.is_file():
            return cand.resolve()

    return p.resolve()



def resolve_batch_path(batch_input: str | Path) -> Path:
    """Smartly resolve batch JSON path across multiple common relative candidate locations."""
    p = Path(batch_input)
    if p.is_file():
        return p.resolve()

    candidates = [
        CASES_DIR / p,
        E2E_DIR / p,
        CASES_DIR / f"{p.name}.json" if not p.suffix else None,
        CASES_DIR / p.name,
    ]
    for cand in candidates:
        if cand and cand.is_file():
            return cand.resolve()

    return p.resolve()

# Pattern to strip leading and trailing punctuation for 100% exact match comparison
PUNCTUATION_PATTERN = re.compile(r"^[。，、！？.!?\s]+|[。，、！？.!?\s]+$")


def resolve_voice(voice_name: str | None) -> str:
    """Resolve a voice alias or full name to a valid edge-tts voice string."""
    if not voice_name:
        return DEFAULT_VOICE
    return SUPPORTED_VOICES.get(voice_name.lower(), voice_name)


def normalize_for_exact_match(text: str) -> str:
    """Normalize text by removing leading/trailing punctuation, extra whitespace, and casing.

    Allows 100% exact match verification against speech recognizers that automatically
    append full stops (。) or commas and vary English letter casing.
    """
    if not text:
        return ""
    cleaned = text.strip()
    cleaned = PUNCTUATION_PATTERN.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned.lower()


def compute_case_cache_key(
    case_id: str,
    text: str = "",
    voice: str = DEFAULT_VOICE,
    rate: str = "+0%",
    volume: str = "+0%",
    pitch: str = "+0Hz",
    batch_name: str | None = None,
) -> str:
    """Compute a clean human-readable filename for the audio fixture without hash codes."""
    safe_id = re.sub(r"[^\w\-]", "_", case_id.strip())
    if batch_name:
        safe_batch = re.sub(r"[^\w\-]", "_", batch_name.strip())
        return f"{safe_batch}_{safe_id}.wav"
    return f"{safe_id}.wav"


def transcode_mp3_to_wav(mp3_bytes: bytes, output_wav_path: Path) -> Path:
    """Transcode raw MP3 bytes to standard lossless 16kHz 16-bit Mono PCM WAV via imageio-ffmpeg."""
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    output_wav_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        ffmpeg_exe,
        "-y",
        "-i",
        "pipe:0",
        "-ar",
        "16000",
        "-ac",
        "1",
        "-f",
        "wav",
        str(output_wav_path),
    ]
    result = subprocess.run(cmd, input=mp3_bytes, capture_output=True)
    if result.returncode != 0:
        err_msg = result.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"FFmpeg transcode to WAV failed (code {result.returncode}): {err_msg}")

    return output_wav_path


async def synthesize_speech(
    text: str,
    output_path: Path,
    voice: str = DEFAULT_VOICE,
    rate: str = "+0%",
    volume: str = "+0%",
    pitch: str = "+0Hz",
) -> Path:
    """Synthesize speech using edge-tts and transcode to standard 16kHz Mono PCM WAV."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_voice = resolve_voice(voice)

    # 防禦微軟伺服器限制：若音色不支援 prosody 調整，自動退回 +0% 避免 NoAudioReceived
    effective_rate = rate
    effective_volume = volume
    effective_pitch = pitch
    if resolved_voice in VOICES_WITHOUT_PROSODY and (
        rate != "+0%" or volume != "+0%" or pitch != "+0Hz"
    ):
        logger.info(
            "音色 '%s' 不支援 prosody 調整，自動退回預設參數 (+0%%/+0Hz)",
            resolved_voice,
        )
        effective_rate = "+0%"
        effective_volume = "+0%"
        effective_pitch = "+0Hz"

    communicate = edge_tts.Communicate(
        text=text.strip(),
        voice=resolved_voice,
        rate=effective_rate,
        volume=effective_volume,
        pitch=effective_pitch,
    )

    chunks: list[bytes] = []
    async for message in communicate.stream():
        if message["type"] == "audio":
            chunks.append(message["data"])

    raw_mp3_bytes = b"".join(chunks)
    if not raw_mp3_bytes:
        raise ValueError(f"No audio received from edge-tts for text: '{text}'")

    # Transcode to lossless 16kHz 16-bit Mono PCM WAV
    return transcode_mp3_to_wav(raw_mp3_bytes, output_path)


async def ensure_audio_fixture(
    case_id: str,
    text: str,
    voice: str = DEFAULT_VOICE,
    rate: str = "+0%",
    volume: str = "+0%",
    pitch: str = "+0Hz",
    output_dir: Path | None = None,
    batch_name: str | None = None,
) -> Path:
    """Ensure audio fixture exists as clean standard .wav with metadata cache invalidation."""
    target_dir = output_dir or GENERATED_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = compute_case_cache_key(
        case_id=case_id,
        text=text,
        voice=voice,
        rate=rate,
        volume=volume,
        pitch=pitch,
        batch_name=batch_name,
    )
    target_path = target_dir / filename

    fingerprint = hashlib.sha256(
        f"{batch_name or ''}:{case_id}:{voice}:{rate}:{volume}:{pitch}:{text.strip()}".encode(
            "utf-8"
        )
    ).hexdigest()

    index_path = target_dir / ".cache_index.json"
    cache_index: dict[str, str] = {}
    if index_path.exists():
        try:
            cache_index = json.loads(index_path.read_text(encoding="utf-8"))
        except Exception:
            cache_index = {}

    if (
        target_path.exists()
        and target_path.stat().st_size > 0
        and cache_index.get(filename) == fingerprint
    ):
        logger.debug("Cache hit for audio fixture: %s", target_path)
        return target_path

    logger.info("Cache miss, synthesizing speech for [%s]: '%s' -> %s", case_id, text, filename)
    await synthesize_speech(
        text=text,
        output_path=target_path,
        voice=voice,
        rate=rate,
        volume=volume,
        pitch=pitch,
    )
    cache_index[filename] = fingerprint
    try:
        index_path.write_text(
            json.dumps(cache_index, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass

    return target_path


def get_audio_upload_payload(audio_path: Path) -> tuple[str, bytes, str]:
    """Return (filename, bytes, content_type) matching the true container of the audio."""
    ext = audio_path.suffix.lstrip(".").lower()
    audio_bytes = audio_path.read_bytes()
    if ext == "wav":
        return ("recording.wav", audio_bytes, "audio/wav")
    if ext == "m4a":
        return ("recording.m4a", audio_bytes, "audio/m4a")
    if ext == "mp3":
        return ("recording.mp3", audio_bytes, "audio/mpeg")
    return (f"recording.{ext}", audio_bytes, f"audio/{ext}")


def generate_random_acoustic_profile() -> dict[str, str]:
    """Generate realistic random acoustic perturbations within safe human speech limits."""
    # 隨機擾動測試優先從支援完整 prosody 調整的音色池中選取
    voice_pool = [v for v in SUPPORTED_VOICES.values() if v not in VOICES_WITHOUT_PROSODY]
    rate_val = random.randint(-15, 25)
    volume_val = random.randint(-25, 10)
    pitch_val = random.randint(-20, 20)
    return {
        "voice": random.choice(voice_pool),
        "rate": f"{rate_val:+d}%",
        "volume": f"{volume_val:+d}%",
        "pitch": f"{pitch_val:+d}Hz",
    }


def load_batch_cases(batch_path: Path | str) -> list[dict[str, str]]:
    """Load and validate test cases from a JSON batch file."""
    target_path = resolve_batch_path(batch_path)
    if not target_path.exists():
        raise FileNotFoundError(f"Batch file not found: {batch_path}")

    raw_data = json.loads(target_path.read_text(encoding="utf-8"))
    if not isinstance(raw_data, list):
        raise ValueError(f"Batch file root must be a JSON array: {target_path}")

    default_batch_name = target_path.stem
    validated_cases: list[dict[str, str]] = []
    for idx, item in enumerate(raw_data):
        if not isinstance(item, dict):
            raise ValueError(f"Case at index {idx} must be a JSON object: {item}")
        case_id = str(item.get("id", f"TC_{idx + 1:03d}")).strip()
        text = str(item.get("text", "")).strip()
        if not text:
            raise ValueError(f"Case '{case_id}' missing non-empty 'text' field")

        validated_cases.append(
            {
                "id": case_id,
                "text": text,
                "batch_name": str(item.get("batch_name", default_batch_name)).strip(),
                "voice": resolve_voice(item.get("voice")),
                "rate": str(item.get("rate", "+0%")).strip(),
                "volume": str(item.get("volume", "+0%")).strip(),
                "pitch": str(item.get("pitch", "+0Hz")).strip(),
            }
        )

    return validated_cases


async def synthesize_batch(
    batch_file: Path,
    output_dir: Path | None = None,
    force: bool = False,
) -> list[Path]:
    """Read a JSON batch case file and synthesize all cases into standard WAV files."""
    cases = load_batch_cases(batch_file)
    out_dir = output_dir or GENERATED_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    if force:
        index_path = out_dir / ".cache_index.json"
        if index_path.exists():
            index_path.unlink()

    results: list[Path] = []
    print(f"[TTS] 開始批次合成音檔: {batch_file.name} (共 {len(cases)} 筆語句)")
    for idx, case in enumerate(cases, 1):
        case_id = case["id"]
        text = case["text"]
        batch_name = case.get("batch_name", batch_file.stem)
        voice = case.get("voice", DEFAULT_VOICE)
        rate = case.get("rate", "+0%")
        volume = case.get("volume", "+0%")
        pitch = case.get("pitch", "+0Hz")

        path = await ensure_audio_fixture(
            case_id=case_id,
            text=text,
            voice=voice,
            rate=rate,
            volume=volume,
            pitch=pitch,
            output_dir=out_dir,
            batch_name=batch_name,
        )
        results.append(path)
        print(
            f"  [{idx}/{len(cases)}] {case_id} -> {path.name} "
            f"({path.stat().st_size:,} bytes, 語音: {voice})"
        )

    print(f"[TTS] 合成作業完成，儲存目錄: {out_dir}")
    return results


if __name__ == "__main__":
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(
        description="依據 JSON 案例檔案批次呼叫 edge-tts 合成標準 16kHz PCM WAV 音訊"
    )
    parser.add_argument(
        "--batch",
        nargs="+",
        default=["cases/parse_voice_1.json"],
        help="JSON 案例檔案路徑 (預設: cases/parse_voice_1.json，可傳入多個檔案)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="強制重新合成所有音訊 (忽略快取)",
    )
    args = parser.parse_args()

    for item in args.batch:
        batch_p = resolve_batch_path(item)
        asyncio.run(synthesize_batch(batch_p, force=args.force))

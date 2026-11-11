"""Standalone CLI Speech E2E Latency and Accuracy Benchmark Runner.

Usage:
    uv run --directory backend python tests/speech_e2e/benchmark.py [cases/parse_voice_1.json ...]
    uv run --directory backend python tests/speech_e2e/benchmark.py cases/parse_voice_2.json cases/parse_voice_3.json
    uv run --directory backend python tests/speech_e2e/benchmark.py --url http://127.0.0.1:8000
    uv run --directory backend python tests/speech_e2e/benchmark.py --voice zh_mixed_female
    uv run --directory backend python tests/speech_e2e/benchmark.py --randomize
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

# Add backend directory to sys.path
BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import httpx  # noqa: E402

from app.main import app  # noqa: E402
from tests.speech_e2e.speech_tts import (  # noqa: E402
    CASES_DIR,
    DEFAULT_VOICE,
    GENERATED_DIR,
    SUPPORTED_VOICES,
    ensure_audio_fixture,
    generate_random_acoustic_profile,
    get_audio_upload_payload,
    load_batch_cases,
    normalize_for_exact_match,
    resolve_batch_path,
    resolve_voice,
)


async def is_server_listening(base_url: str) -> bool:
    """Check if external FastAPI server is running on base_url."""
    try:
        async with httpx.AsyncClient(timeout=1.0) as client:
            res = await client.get(f"{base_url.rstrip('/')}/health")
            return res.status_code == 200
    except Exception:
        return False


async def run_benchmark(
    batch_paths: Path | list[Path],
    base_url: str | None = None,
    voice_override: str | None = None,
    rate_override: str | None = None,
    volume_override: str | None = None,
    pitch_override: str | None = None,
    randomize: bool = False,
) -> int:
    resolved_paths = [batch_paths] if isinstance(batch_paths, Path) else list(batch_paths)
    cases: list[dict[str, str]] = []
    for p in resolved_paths:
        cases.extend(load_batch_cases(p))

    batch_display = ", ".join(p.name for p in resolved_paths)
    print("=" * 70)
    print("Nestock A2 語音端到端非批次單次精確計時與 100% 準確度測試 (16kHz PCM WAV)")
    print(f"批次檔案: {batch_display} (共 {len(cases)} 筆語句)")
    if randomize:
        print("[OPTION] 啟用隨機聲學擾動打靶模式 (--randomize)")
    print("=" * 70)

    # Determine transport: live external server vs in-process ASGI
    use_external = False
    target_url = base_url or "http://127.0.0.1:8000"
    if await is_server_listening(target_url):
        use_external = True
        print(f"[MODE] 偵測到外部執行中後端服務，連線至: {target_url}")
    else:
        print("[MODE] 未偵測到本機運行中之 8000 埠服務，以內建 ASGI 核心模式執行全鏈路測試")

    latencies_ms: list[float] = []
    passed_cases = 0
    failed_cases = 0

    transport = None if use_external else httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url=target_url if use_external else "http://testserver",
        timeout=45.0,
    ) as client:
        for idx, case in enumerate(cases, 1):
            case_id = case["id"]
            expected_text = case["text"]

            # Acoustic parameters resolution
            if randomize:
                rand_profile = generate_random_acoustic_profile()
                effective_voice = rand_profile["voice"]
                effective_rate = rand_profile["rate"]
                effective_volume = rand_profile["volume"]
                effective_pitch = rand_profile["pitch"]
            else:
                effective_voice = resolve_voice(voice_override or case.get("voice", DEFAULT_VOICE))
                effective_rate = rate_override or case.get("rate", "+0%")
                effective_volume = volume_override or case.get("volume", "+0%")
                effective_pitch = pitch_override or case.get("pitch", "+0Hz")

            case_batch = case.get("batch_name", resolved_paths[0].stem)
            print(f"\n[{idx}/{len(cases)}] 執行案例: [{case_batch}] {case_id}")
            print(f'  * 原始輸入語句: "{expected_text}"')
            print(
                f"  * 聲學參數配置: 音色={effective_voice}, 語速={effective_rate}, "
                f"音量={effective_volume}, 頻率={effective_pitch}"
            )

            # 1. Ensure synthesized genuine WAV fixture is ready
            audio_path = await ensure_audio_fixture(
                case_id=case_id,
                text=expected_text,
                voice=effective_voice,
                rate=effective_rate,
                volume=effective_volume,
                pitch=effective_pitch,
                output_dir=GENERATED_DIR,
                batch_name=case_batch,
            )
            filename, audio_bytes, content_type = get_audio_upload_payload(audio_path)
            print(f"  * 測試音訊載具: {filename} ({len(audio_bytes):,} bytes, {content_type})")

            # 2. Monotonic high-resolution timing probe (T0)
            t0 = time.perf_counter()

            # 3. Single-turn HTTP POST request
            files = {"file": (filename, audio_bytes, content_type)}
            try:
                response = await client.post("/api/speech/transcribe", files=files)
                t1 = time.perf_counter()
                latency_ms = (t1 - t0) * 1000.0
                latencies_ms.append(latency_ms)

                if response.status_code != 200:
                    print(f"  * HTTP 狀態碼:  [FAIL] {response.status_code} ({response.text})")
                    failed_cases += 1
                    continue

                data = response.json()
                raw_transcript = data.get("transcript", "")
                duration_seconds = data.get("duration_seconds")
                norm_transcript = normalize_for_exact_match(raw_transcript)
                norm_expected = normalize_for_exact_match(expected_text)

                print(f'  * 轉錄回傳文字: "{raw_transcript}"')
                print(f"  * 單次端到端耗時: {latency_ms:.2f} ms")
                if duration_seconds:
                    rtf = (latency_ms / 1000.0) / duration_seconds
                    print(f"  * 即時因子 (RTF): {rtf:.2f} (音訊時長: {duration_seconds:.2f}s)")

                # 4. Strict 100% exact match assertion
                if norm_transcript == norm_expected:
                    print("  * 100% 準確度比對: [PASS] 完全相符")
                    passed_cases += 1
                else:
                    print("  * 100% 準確度比對: [FAIL] 辨識不符！")
                    print(f'    - 預期內容: "{norm_expected}"')
                    print(f'    - 實際轉錄: "{norm_transcript}"')
                    failed_cases += 1

            except Exception as exc:
                print(f"  * 呼叫異常失敗: {type(exc).__name__} - {exc}")
                failed_cases += 1

    # Summary Report
    avg_latency = sum(latencies_ms) / len(latencies_ms) if latencies_ms else 0.0
    min_latency = min(latencies_ms) if latencies_ms else 0.0
    max_latency = max(latencies_ms) if latencies_ms else 0.0
    accuracy_rate = (passed_cases / len(cases) * 100.0) if cases else 0.0

    print("\n" + "=" * 70)
    print(f"測試總結報告: {batch_display}")
    print("=" * 70)
    print(f"  * 總案例數量:   {len(cases)}")
    print(f"  * 成功案例數:   {passed_cases} ([PASS])")
    print(f"  * 失敗案例數:   {failed_cases} ([FAIL])")
    print(f"  * 100% 準確率:  {accuracy_rate:.1f}%")
    print(f"  * 平均延遲時間: {avg_latency:.2f} ms")
    print(f"  * 最低延遲時間: {min_latency:.2f} ms")
    print(f"  * 最高延遲時間: {max_latency:.2f} ms")
    print("=" * 70)

    return 0 if failed_cases == 0 else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Speech E2E Benchmark CLI (16kHz PCM WAV)")
    parser.add_argument(
        "batch_files",
        nargs="*",
        default=[str(CASES_DIR / "parse_voice_1.json")],
        help="Path to JSON batch file(s) (e.g. cases/parse_voice_2.json cases/parse_voice_3.json)",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="Base URL of backend server (e.g. http://127.0.0.1:8000)",
    )
    parser.add_argument(
        "--voice",
        choices=list(SUPPORTED_VOICES.keys()) + list(SUPPORTED_VOICES.values()),
        default=None,
        help="全域覆蓋發音角色 (如 zh_mixed_female, zh_tw_male)",
    )
    parser.add_argument(
        "--rate",
        default=None,
        help="全域覆蓋語速 (例: +20%%, -15%%)",
    )
    parser.add_argument(
        "--volume",
        default=None,
        help="全域覆蓋音量 (例: -20%%, +10%%)",
    )
    parser.add_argument(
        "--pitch",
        default=None,
        help="全域覆蓋頻率 (例: +20Hz, -15Hz)",
    )
    parser.add_argument(
        "--randomize",
        action="store_true",
        help="啟用自然人聲範圍隨機聲學擾動打靶",
    )
    args = parser.parse_args()

    files = args.batch_files or [str(CASES_DIR / "parse_voice_1.json")]
    batch_paths = [resolve_batch_path(f) for f in files]
    exit_code = asyncio.run(
        run_benchmark(
            batch_paths=batch_paths,
            base_url=args.url,
            voice_override=args.voice,
            rate_override=args.rate,
            volume_override=args.volume,
            pitch_override=args.pitch,
            randomize=args.randomize,
        )
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

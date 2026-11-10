"""手動真實語音辨識測試工具 (Manual Speech Live Test Runner).

用法：
1. 執行預設的 6 種內建語音模式測試（繁中查詢、中英混雜、英文品牌、多品項、WAV 格式、極短靜音）：
   uv run --directory backend python tests/manual_test_speech.py

2. 測試您自己自訂的錄音檔（支援 M4A, AAC, WAV, MP3）：
   uv run --directory backend python tests/manual_test_speech.py path/to/voice.m4a [language]
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.speech import (  # noqa: E402
    AzureSpeechService,
    MockSpeechToTextService,
    SpeechToTextService,
    load_speech_azure_settings,
)

FIXTURES_DIR = BACKEND_DIR / "tests" / "fixtures" / "speech"


async def run_single_test(
    service: SpeechToTextService,
    audio_path: Path,
    language: str | None = None,
    scenario_title: str = "",
) -> None:
    if not audio_path.exists():
        print(f"[ERROR] 找不到測試音訊檔案: {audio_path}")
        return

    payload_bytes = audio_path.read_bytes()
    ext = audio_path.suffix.lstrip(".").lower()
    content_type = f"audio/{ext}" if ext != "wav" else "audio/wav"

    print("\n" + "=" * 65)
    print(f"測試項目: {scenario_title or audio_path.name}")
    print(f"檔案路徑: {audio_path}")
    print(f"檔案大小: {len(payload_bytes):,} bytes")
    print(f"指定語言: {language or '預設繁中候選 (zh-TW，原生支援中英混雜與英文品名)'}")
    print("-" * 65)

    try:
        res = await service.transcribe(
            audio_bytes=payload_bytes,
            content_type=content_type,
            language=language,
        )
        provider_name = (
            "Azure AI Speech (Fast Transcription)"
            if isinstance(service, AzureSpeechService)
            else "Mock Speech (範例模式)"
        )
        print(f"[SUCCESS] {provider_name} 轉錄完成！")
        print(f"  * 轉錄文字 (transcript)       : {res.transcript or '(空字串/靜音/無辨識結果)'}")
        print(f"  * 判定語言 (detected_language): {res.detected_language}")
        print(f"  * 音訊時長 (duration_seconds) : {res.duration_seconds} 秒")
    except Exception as exc:
        print(f"[FAILED] 呼叫失敗: {type(exc).__name__} - {exc}")
        import traceback

        traceback.print_exc()


async def main() -> None:
    from dotenv import load_dotenv

    load_dotenv(BACKEND_DIR / ".env", override=False)

    print("=" * 65)
    print("Nestock A2 語音辨識模型手動測試器 (Speech Test Runner)")
    print("=" * 65)

    settings = load_speech_azure_settings()

    if settings is not None:
        print("[MODE] 偵測到 Azure AI Speech 設定，啟動【真實連線模式 (Live Azure AI Speech)】：")
        print(f"  * 服務區域 (Region): {settings.service_region}")
        if settings.endpoint:
            print(f"  * 服務端點 (Endpoint): {settings.endpoint}")
        service: SpeechToTextService = AzureSpeechService(settings=settings, mock_fallback=False)
    else:
        print("[MODE] 未偵測到 Azure 設定，自動啟用【範例模式 (MockSpeechToTextService)】：")
        print("  * 說明：以內建語音示範邏輯進行確定性回傳，無需連網或金鑰。")
        service = MockSpeechToTextService()

    # 若命令列有指定音訊參數：測試使用者自選錄音檔
    if len(sys.argv) > 1:
        custom_path = Path(sys.argv[1]).resolve()
        custom_lang = sys.argv[2] if len(sys.argv) > 2 else None
        await run_single_test(
            service,
            custom_path,
            custom_lang,
            f"自訂使用者測試音訊 ({custom_path.name})",
        )
        return

    # 預設執行 6 種內建 fixture 模式測試
    tests = [
        (
            FIXTURES_DIR / "voice_zh_tw_query.wav",
            None,
            "模式 1：真實繁體中文生活查詢 (幫我查一下衛生紙)",
        ),
        (
            FIXTURES_DIR / "voice_code_switching.wav",
            None,
            "模式 2：真實中英混雜語音 (買兩罐 Oatly 燕麥奶)",
        ),
        (
            FIXTURES_DIR / "voice_en_brand.wav",
            None,
            "模式 3：真實英文品牌與商品 (Buy two Kirkland paper towels)",
        ),
        (
            FIXTURES_DIR / "voice_multi_items.wav",
            None,
            "模式 4：真實多品項清單連續語音 (鮮奶兩瓶、雞蛋一盒、醬油一罐)",
        ),
        (
            FIXTURES_DIR / "voice_standard_pcm.wav",
            None,
            "模式 5：真實標準 PCM WAV 格式音訊 (庫存還有多少可口可樂)",
        ),
        (
            FIXTURES_DIR / "voice_silence_short.m4a",
            None,
            "模式 6：極短靜音/邊界防禦音訊 (<64 bytes)",
        ),
    ]

    for audio_path, lang, title in tests:
        await run_single_test(service, audio_path, lang, title)

    print("\n" + "=" * 65)
    print("手動測試全數執行完畢！")
    print("=" * 65)


if __name__ == "__main__":
    asyncio.run(main())

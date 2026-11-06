"""手動真實視覺辨識測試工具 (Manual Vision Live Test Runner).

用法：
1. 執行預設的 3 種內建測試圖（單一商品、收據發票、櫥櫃多品項）：
   uv run --directory backend python tests/manual_test_vision.py

2. 測試您自己自訂的照片（支援任意外部圖片）：
   uv run --directory backend python tests/manual_test_vision.py path/to/photo.jpg [scene_type]
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.dependencies import get_inventory_service  # noqa: E402
from app.models.vision import SceneType  # noqa: E402
from app.services.provisional_storage.local_staging_adapter import LocalStagingMediaStorage  # noqa: E402
from app.services.vision import (  # noqa: E402
    AzureOpenAIVisionService,
    MockVisionService,
    VisionService,
    load_vision_azure_settings,
)

FIXTURES_DIR = BACKEND_DIR / "tests" / "fixtures" / "vision"


async def run_single_test(
    service: VisionService,
    media: LocalStagingMediaStorage,
    household_id: str,
    image_path: Path,
    scene_hint: SceneType | None = None,
    scenario_title: str = "",
) -> None:
    if not image_path.exists():
        print(f"[ERROR] 找不到測試圖片檔案: {image_path}")
        return

    payload_bytes = image_path.read_bytes()
    image_ref = image_path.name

    print("\n" + "=" * 65)
    print(f"測試項目: {scenario_title or image_path.name}")
    print(f"檔案路徑: {image_path}")
    print(f"圖片大小: {len(payload_bytes):,} bytes")
    hint_str = scene_hint.value if scene_hint else "無 (由模型自主推斷)"
    print(f"場景提示 (scene_hint): {hint_str}")
    print("-" * 65)

    # 模擬客戶端將圖片上傳至 Staging 儲存層
    await media.simulate_client_upload(household_id, image_ref, payload_bytes)

    try:
        result = await service.analyze_image(household_id, image_ref, scene_type=scene_hint)
        provider_name = (
            "Azure OpenAI Vision"
            if isinstance(service, AzureOpenAIVisionService)
            else "Mock Vision (範例模式)"
        )
        print(f"[SUCCESS] {provider_name} 解析成功！")
        print(f"  * 推斷場景類別 (scene_type) : {result.scene_type.value}")
        print(f"  * 原始 OCR 萃取 (raw_text)  : {result.raw_text or '(無文字)'}")
        print(f"  * 辨識品項數量 (items count): {len(result.detected_items)} 件")
        print("\n  [品項結構化辨識清單]")
        for idx, item in enumerate(result.detected_items, 1):
            unit_display = item.unit if item.unit else "(無單位/需澄清)"
            confirmed_mark = "[V] 確定" if item.unit_confirmed else "[?] 待確認"
            matched_id_str = item.matched_inventory_id or "無"
            print(f"    [{idx}] 標準品名: {item.canonical_name}")
            print(f"        原始字樣: {item.raw_detected_name}")
            print(f"        估算數量: {item.quantity} {unit_display} ({confirmed_mark})")
            print(f"        特徵標籤: {item.feature_keywords}")
            print(f"        信心指標: {item.confidence:.2f}")
            print(f"        庫存比對: {item.match_status.value} (命中ID: {matched_id_str})")
            if item.candidate_matches:
                candidates_str = ", ".join(
                    f"{c.name}({c.score:.2f})" for c in item.candidate_matches
                )
                print(f"        模糊候選: [{candidates_str}]")
    except Exception as exc:
        print(f"[FAILED] 呼叫失敗: {type(exc).__name__} - {exc}")
        import traceback

        traceback.print_exc()


async def main() -> None:
    from dotenv import load_dotenv

    load_dotenv(BACKEND_DIR / ".env", override=False)

    print("=" * 65)
    print("Nestock A2 視覺模型手動測試器 (Vision Test Runner)")
    print("=" * 65)

    settings = load_vision_azure_settings()
    media = LocalStagingMediaStorage()
    inventory = get_inventory_service()
    household_id = "demo-household"

    if settings is not None:
        print("[MODE] 偵測到 Azure OpenAI 設定，啟動【真實連線模式 (Live Azure OpenAI)】：")
        print(f"  * 連線端點 (Endpoint)  : {settings.endpoint}")
        print(f"  * 使用模型 (Deployment): {settings.deployment}")
        print(f"  * 基礎網址 (Base URL)  : {settings.base_url}")
        service: VisionService = AzureOpenAIVisionService(
            settings, inventory, media, mock_fallback=False
        )
    else:
        print("[MODE] 未偵測到 Azure OpenAI 設定，自動啟用【範例模式 (MockVisionService)】：")
        print("  * 說明：以內建生活用品範例資料進行結構化展示，無需連網或金鑰。")
        service = MockVisionService(inventory_service=inventory)

    # 若命令列有指定圖片參數：測試使用者自選圖
    if len(sys.argv) > 1:
        custom_path = Path(sys.argv[1]).resolve()
        custom_hint = None
        if len(sys.argv) > 2:
            try:
                custom_hint = SceneType(sys.argv[2])
            except ValueError:
                print(f"[WARN] 未知的 scene_type: {sys.argv[2]}，將留空讓模型自主判斷。")
        await run_single_test(
            service,
            media,
            household_id,
            custom_path,
            custom_hint,
            f"自訂使用者測試圖片 ({custom_path.name})",
        )
        return

    # 預設執行 3 種內建 fixture 場景測試
    tests = [
        (
            FIXTURES_DIR / "img_single.jpg",
            None,
            "場景 1：單一商品 (無提示，由模型自主推斷 scene_type 與清洗行銷贅字)",
        ),
        (
            FIXTURES_DIR / "146078.jpg",
            SceneType.RECEIPT,
            "場景 2：收據發票 (指定 receipt，多品項萃取、過濾發票金額與稅金)",
        ),
        (
            FIXTURES_DIR / "img_pantry.jpg",
            SceneType.PANTRY_SCENE,
            "場景 3：儲藏櫃/冰箱 (指定 pantry_scene，多目標物體識別與包裝量綱解析)",
        ),
    ]

    for img_path, hint, title in tests:
        await run_single_test(service, media, household_id, img_path, hint, title)

    print("\n" + "=" * 65)
    print("手動測試全數執行完畢！")
    print("=" * 65)


if __name__ == "__main__":
    asyncio.run(main())

# B2 驗收證據（checklist §7 用）

## 2026-11-18 更新：revision 13（Issue #176 照片附加）

> 日期：2026-11-18（台北凌晨，Demo 前一晚部署並驗證）。
> Backend revision：`nestock-api--0000013`，映像 `nestockacr26530.azurecr.io/nestock-api:20261118001356-718ad2d`，流量 100%，單一副本，狀態 Healthy。revision `0000012` 留作回滾（流量 0%）。
> 程式：`origin/main` `718ad2d`，含 PR #199、#200、#201、#203。部署只更新映像，未設 `SECRETS_FILE`，既有 secret 與環境變數（含 11/17 設定的 Blob SAS）沒有改動。
> Base URL：`https://nestock-api.ambitiousgrass-c37169f5.eastasia.azurecontainerapps.io`
> Household：`demo-household`。這次有一次寫入，見下方「資料變動」。

### 七步 smoke test：全過

腳本是 `feature/b2-smoke-vision-step`（`4996c80`）的 `deploy/smoke_test.py`，**尚未合併到 `main`**。它與 `main` 上六步版本的差別：加上第 7 步照片辨識，並拿掉固定的 Demo baseline，第 2 到 4 步改成檢查資料一致性（同一個 household、沒有重複、預測不改動確認數量）。原因：`demo-household` 在測試階段已有許多測試品項（29 筆，原 baseline 為 7 筆），`main` 的六步版本會在第 2 步失敗，那是資料差異，不是部署問題。

| 步驟 | 結果 | 證據 |
| --- | --- | --- |
| 1 `/health` | PASS | `status ok` |
| 2 `/inventory` | PASS | 29 筆、同一個 household、沒有重複 |
| 3 `/shopping` | PASS | 2 筆（皆 pending）、同一個 household、沒有重複 |
| 4 `/forecast` | PASS | 29 筆（5 筆有預測），確認數量未被預測動過 |
| 5 speech | PASS | 真實逐字稿「洗衣精還有幾瓶？」（非 mock） |
| 6 agent | PASS | 問「電池」，答案帶數量 4 |
| 7 photo recognition | PASS | 辨識出 3 樣（QUAKER OLD FASHIONED OATS、蘋果、Campbell's CONDENSED Tomato SOUP），測試卡片已自動取消，沒有新增品項 |

`main` 上六步版本（固定 baseline）在同一個 revision 的結果：第 1 步通過，第 2 步失敗（29 筆對 baseline 7 筆）。這是已知的測試階段資料差異，已由上面七步版本取代。

### Issue #176 照片附加：路徑 A（拍照新增品項）雲端端到端

用真實照片（購物籃，`backend/tests/fixtures/vision/1280.jpg`），在雲端實際跑：

| 檢查 | 結果 |
| --- | --- |
| 出卡：6 行，每一行都帶 `image_ref`、`input_id`、`mapping_etag` | PASS |
| 只確認其中 1 行（HAIR RECIPE 無花果洗髮產品，1 瓶），回應 `completed` | PASS |
| 未確認的 5 行沒有寫入 | PASS |
| Inventory 該品項有 `image_ref`，指向同一張照片 | PASS |
| Blob：照片原檔（143,308 bytes）與辨識結果 `vision_v1.json`（2,285 bytes）在 `attached/`；`staging/` 只剩 0 bytes 的資料夾標記 | PASS |
| 再按一次確認：回 `already_completed`，數量仍為 1，品項總數仍為 29，沒有多寫 | PASS |

照片搬離 staging 是由 `/agent/resume` 成功後的背景工作完成（PR #203）。work item 的 `completed` 狀態**沒有直接讀 Cosmos**，只由「照片已在 `attached/`」間接證明。

### 資料變動（`demo-household`）

- 新增 1 個品項：HAIR RECIPE 無花果洗髮產品 1 瓶（`item_id` `49ea0749-9fa2-52e6-9b75-e69a2ebb71d9`，有照片），品項總數 28 → 29。除此之外這次驗證沒有寫入，也沒有 reset。
- 另有 4 個品項（無糖高纖豆漿、兩個 ARIEL 洗衣精、雪芙蘭面膜）的 `image_ref` 是 A1 用本機後端測試留下的，雲端 Blob 沒有這些檔案（整個 `media` 容器搜尋為 0 bytes）。Demo 不使用這些照片，已確認。
- `staging/` 另有 24 張未附加的測試照片，依 Lifecycle rule（建立超過 1 天刪除）會被自動清除，最早約台北 11/18 12:14 之後。已附加的照片在 `attached/`，不受影響。

### 尚未完成（Azure 於 12/3 後關閉，兩條端到端路徑須於 12/3 前完成）

- **路徑 A（拍照新增品項）**：雲端已驗證（上表）。
- **路徑 B（拍照購買）**：**雲端尚未驗證。** A1 在 11/17 用本機後端連真實 Cosmos 測過，但該次用的是已有照片的豆漿，走「不換照片」；「購買並附新照片」尚無雲端證據。須在 12/3 前補做。
- work item `completed` 狀態未直接讀取。
- 全程只用 `demo-household` 與合成或測試照片；Android 實機操作未驗證。
- `deploy/smoke_test.py` 的七步版本尚未在 `main`；Demo 當天（含 B1 備援）要跑七步版本，須先合併。
- Demo 資料不是原始 baseline（見上）；若 Demo 前要回到 7 筆，需資料 owner 授權（#126），B1 在 #156 已決定本次不 reset。

## 2026-11-15 紀錄（revision `0000004`，歷史，未更新）

> 日期：2026-11-15。Backend revision：`nestock-api--0000004`（流量 100%，單一副本）。
> Base URL：`https://nestock-api.ambitiousgrass-c37169f5.eastasia.azurecontainerapps.io`
> Household：`demo-household`。以下都是唯讀驗證，沒有寫入或 reset。

| 項目 | 結果 | 證據 |
| --- | --- | --- |
| 雲端 smoke test | PASS | `deploy/smoke_test.py` 六步全過：health、inventory 7 筆、shopping 3 筆、forecast、真實 speech 逐字稿、agent |
| baseline 預檢（loader，不加 `--apply`） | PASS | `action=already_loaded applied=False records_planned=0 documents_deleted=0` |
| AI 端點（本機 `.env` 執行） | PASS | `check_ai_endpoints.py`：`NESTOCK_SPEECH_MOCK` 未設定、OpenAI、Vision、Speech 皆通過 |
| Forecast：洗衣精 | PASS | `estimated`、`consider_restock`、剩約 3 天 |
| Forecast：衛生紙 | PASS | `estimated`、`none`、8 包、剩約 40 天 |
| Forecast：資料不足 | PASS | 電池、鹽、糖為 `insufficient_data`，無編造天數 |
| Agent：「衛生紙還能用多久？」 | PASS | 「目前有 8 包，預估還能用約 40 天」，無 `pending_approval` |
| Agent：「最近有什麼快用完？」 | PASS | 麵粉（0 包）、洗衣精（剩 1 瓶、約 3 天）；另 3 項紀錄不足 |
| 預測不覆寫庫存 | PASS | `app/services/forecast.py` 只讀庫存數量（`confirmed_quantity=item.quantity`），沒有寫入；forecast 相關測試 70 passed；雲端 `/inventory` 數量仍等於 baseline（洗衣精 1、衛生紙 8） |
| 舊 admin 密碼 secret 已移除 | PASS | 已移除 Container App secret `nestockacr26530azurecrio-nestockacr26530`（registry 設定與環境變數都沒有引用它）；revision 仍為 `0000004`，移除後 smoke test 六步全過 |
| ACR 安全 | PASS | `adminUserEnabled=false`；以 pull-only token `nestock-api-pull` 拉取（scope 僅 `content/read`、`metadata/read`，密碼到期 2026-12-15） |

## 尚未驗證

- **Live reset**：#126 尚無 data owner 授權，不執行 `--apply --reset`。
- **雲端執行環境的 Vision**：`check_ai_endpoints.py` 是在本機跑的；雲端的 Vision 連線尚無獨立證據（雲端 Agent 與 Speech 已通過）。**2026-11-18 已補上：** 七步 smoke test 第 7 步與拍照新增的雲端端到端皆通過，見上方更新。
- **Android 實機、C2 是否已收到並設定 base URL**。
- **部署 owner 確認**（checklist 仍標示 tentative）。

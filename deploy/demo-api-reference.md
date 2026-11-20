# Final Demo API 一覽（Android 實機 → 雲端 HTTPS）

> 依據：`demo-readiness-checklist-v3.md`。
> 權威來源仍是 `backend/app/models` 與 FastAPI 產生的 `/openapi.json`；這份只整理
> 「Demo 會用到哪些 API、誰負責、現在做到哪、缺什麼」，不是第二份 schema。
> 狀態以本 checkout（已合併 2026-11-15 的 `origin/main`）為準，並對照雲端 revision
> `nestock-api--0000004` 的 `/openapi.json` 與實際呼叫；其他人的未合併分支不算。
> 最後核對日：2026-11-15。

## 1. 通則

| 項目 | 規定 |
| --- | --- |
| Base URL | `https://nestock-api.ambitiousgrass-c37169f5.eastasia.azurecontainerapps.io`（主要實例，B2）；App 以編譯期設定注入，Final Demo 不得用 localhost / `10.0.2.2` / ADB reverse |
| 範圍鍵 | 所有資料用 `household_id`，Demo 固定 `demo-household`；無登入、無帳號 |
| 格式 | JSON；上傳音檔用 `multipart/form-data`；圖片用預簽 PUT |
| 寫入 | 只有 `/agent/resume` 經使用者確認後才能寫；其他路由唯讀（資料重設走 B1 的 loader，不是 API，見 runbook） |
| 失敗 | fail-honest：未設定回 501/503，上游失敗回 502，不回假成功 |
| 重試 | 同一邏輯寫入沿用同一個 `operation_id`；App 重試、逾時、遺失回應都不能重複寫入 |

## 2. 端點與 Demo 情境對照

| 端點 | 用途 | 負責 | 本 checkout 狀態 | 對應 Demo 情境 |
| --- | --- | --- | --- | --- |
| `GET /health` | 存活檢查 | 共用 | ✅ 只回 `{"status":"ok"}`，**不檢查任何下游** | A 雲端連線 |
| `GET /inventory` | 庫存列表 | B1 | ✅ 有 Cosmos opt-in | B、D、F、G |
| `GET /shopping` | 待購清單 | B1 / C1 | ✅ 有 Cosmos opt-in | D、F |
| `GET /forecast` | 全家預測 | B2 | ✅ 已完成 | C（洗衣精快用完） |
| `GET /forecast/{item_id}` | 單品預測，找不到回 404 | B2 | ✅ 已完成 | C（衛生紙還能用多久） |
| `GET /forecast/reorder` | 建議補貨清單 | B2 | ✅ 雲端有此路由 | C、低庫存 → 比價 |
| `POST /agent/messages` | Agent；未設 Azure OpenAI 回 501、上游失敗回 502。不寫 domain data；有 Cosmos 時可產生購買確認卡（`complete_purchase`）並**存成待確認紀錄** | A1 | ✅ 雲端問答已驗證（庫存、預測）；確認卡流程**未在雲端實測** | B、C、E、F |
| `POST /agent/resume` | 確認 / 取消確認卡；只寫該卡儲存的內容。需 Cosmos，否則 503 | A1 | ✅ 已接上：雲端對不存在的卡回 404（唯讀探測，未寫入）。**真實確認 / 取消 / 重試未在雲端實測** | F、G、H |
| `POST /api/media/upload-authorization` | 取得上傳授權與 `image_ref` | A2 | ✅ | E |
| `PUT /staging/upload/{token}` | 直接上傳二進位 | A2 | ⚠️ 暫時性實作（見 §4） | E |
| `POST /api/speech/transcribe` | 語音轉文字，≤5MB，m4a/aac/mp4/wav/mp3/webm | A2 | ✅ 雲端已驗證為真實辨識（smoke test 步驟 5） | 語音 |

Agent 請求／回應欄位（`AgentRequest`、`AgentResponse`、`PendingApproval`、`ResumeRequest`）
見 `backend/app/models/agent.py`；`ResumeRequest` 的選擇欄位：
`approved_operation_ids`（批次卡）、`selected_option_id`（比價卡、item_match 卡）。

## 3. 錯誤碼約定（C2 據此顯示可恢復訊息）

| 碼 | 意思 | App 行為 |
| --- | --- | --- |
| 400 / 413 / 415 | 輸入有問題（空檔、太大、格式不支援） | 顯示原因，可重錄 / 重選 |
| 404 | 項目不存在 | 顯示找不到 |
| 410 | 上傳 token 過期 | 重新取得授權 |
| 501 | 該功能這個環境沒開（例如 Agent 未設定 Azure OpenAI） | 顯示「尚未開放」，不重試 |
| 409 | 選擇與該確認卡不符（`/agent/resume`） | 顯示錯誤，不寫入，請重新取得卡片 |
| 502 / 503 | 上游（OpenAI / Speech）失敗或未設定；`/agent/resume` 沒有 Cosmos 時也回 503 | 顯示可重試 |

## 4. 對照 checklist 的 API 缺口（2026-11-15 重新核對）

| # | 缺口 | 現況 | 負責 |
| --- | --- | --- | --- |
| G1 | `/agent/resume` 與確認卡 | **已接上**（有 Cosmos 才可用；雲端對未知卡回 404）。真實的確認、取消、重試冪等，雲端尚未實測 | A1（情境 F/G/H 需 A1 / C1 實測） |
| G2 | `/health` 不檢查下游 | 仍只回 `{"status":"ok"}`；以 `deploy/smoke_test.py` 的分層檢查取代，**未加 `/health/deep`**（不動共用檔） | B2（已用替代方案） |
| G3 | 沒有獨立的 Vision 路由 | 路由表只有 media 上傳與 speech；照片辨識走哪條路徑需 A2 / A1 確認。雲端 Vision 無獨立證據 | A2 + A1 |
| G4 | 暫存上傳狀態在記憶體 | 確認卡已改存 Cosmos；**暫存上傳仍可能在記憶體**（雲端未設 Blob Storage，見 Issue #98） | A2 |
| G5 | 預測時間語意 | **已解決**：雲端預測為 `estimated` / `insufficient_data`，洗衣精剩約 3 天、衛生紙約 40 天 | B2（完成） |
| G6 | 語音可能是 mock | **雲端已解決**：`NESTOCK_SPEECH_MOCK` 不存在，smoke test 取得真實逐字稿 | A2（完成） |
| G7 | 重設 Demo 資料 | B1 的 loader（`app.services.demo_seed`，PR #131）已合併；雲端資料目前 `already_loaded`。**live reset 仍需 #126 授權** | B1 + data owner |

## 5. 部署時的 API 行為約束

- **單一副本**：暫存上傳 token 可能仍在記憶體（確認卡已存 Cosmos），最小與最大副本數都設 1，
  直到 A2 把暫存上傳移出記憶體（Issue #98）。備用實例同樣單一副本，且只在開場前切換（runbook §7）。
- **上傳 URL**：`upload-authorization` 用 request 的 base URL 組 staging 網址，
  所以雲端要正確轉送 `X-Forwarded-Proto/Host`（uvicorn 加 `--proxy-headers`），
  否則回給手機的是 `http://` 內網位址。
- **不需要 CORS**：Android 原生 App 不經瀏覽器。

## 6. `GET /health/deep`：不採用（2026-11-15）

先前提案的 `/health/deep` 需要改 `routes.py` 與 `dependencies.py`（共用 / B1），B2 沒有加入。
採用的替代方案是 `deploy/smoke_test.py`：只打既有端點（`/health`、`/inventory`、`/shopping`、
`/forecast`、`/api/speech/transcribe`、`/agent/messages`、`/agent/open-card`），不改任何共用檔，無 FAIL 才算通過（SKIP 不算通過）。

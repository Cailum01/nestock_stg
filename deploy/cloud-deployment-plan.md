# Final Demo 雲端部署分工

> 依據 `demo-readiness-checklist-v3.md`。狀態核對日：2026-11-15（雲端 revision `nestock-api--0000004`）。
> 操作細節以 `deploy/demo-day-runbook.md` 為準；這份記錄分工、里程碑與風險。

## 1. 架構與現況

```text
Android 實機 ──HTTPS──▶ Azure Container Apps（nestock-api，單一副本）
                          ├─▶ Azure OpenAI（Agent、Vision）
                          ├─▶ Azure Speech
                          └─▶ Cosmos DB（household-data，system-assigned identity，無金鑰）
```

- 主要實例：`https://nestock-api.ambitiousgrass-c37169f5.eastasia.azurecontainerapps.io`（B2 的訂閱）。
- 映像由 ACR 拉取，admin user 關閉；因學生角色只能把角色指派給 User，改用 pull-only token
  （見 runbook §6）。
- 不依賴任何人的筆電登入，也不用 localhost、`10.0.2.2` 或 ADB reverse。

## 2. 分工（2026-11-15 決議）

**雲端部署由任何成員可重現（需 Linux、macOS 或 WSL，不支援 Git Bash；見 runbook §7）。主要實例在 B2，備用實例由 B1 維護。**
不授權他人進 B2 的訂閱；各成員用自己的帳號、訂閱與 endpoint 部署（runbook §7），
所以金鑰不會因為授權而互相看見。

| 角色 | 負責 |
| --- | --- |
| B2 | 主要實例：部署、revision、ACR token 輪替、smoke test、runbook、base URL 交付 |
| B1 | 備用實例；替各實例的 identity 指派 Cosmos 資料角色；Demo 當天開場前跑 smoke test |
| C2 | 把 App 指向 base URL、實機排練；回覆 App 能否不重新打包就更換 base URL |
| A1 / A2 / C1 | 提供各自服務的值與程式正確性；B2 只保證它們部署後可達、設定正確，有 bug 時回報，不代修 |

## 3. 環境變數（只列名稱；值不入 repo）

| 變數 | 用途 | 提供者 |
| --- | --- | --- |
| `NESTOCK_COSMOS_ENDPOINT` / `_DATABASE` / `_CONTAINER` | 啟用 Cosmos（認證用 identity，不需金鑰） | B1 |
| `AZURE_OPENAI_ENDPOINT` / `_API_KEY` / `_DEPLOYMENT` | `/agent/messages`（未設回 501） | A1 |
| `AZURE_SPEECH_KEY` / `_REGION` | 語音轉文字 | A2 |
| `NESTOCK_STAGING_BASE_URL` | 暫存上傳網址覆寫（proxy header 不可靠時才用） | A2 |
| `NESTOCK_SPEECH_MOCK` | **Final Demo 必須不存在** | A2 |

## 4. Demo 資料重設

重設與讀回一律使用 B1 的 loader（`python -m app.services.demo_seed`，PR #131），B2 不另寫刪除或寫入實作。

1. **唯讀預檢**（不加 `--apply`）：2026-11-15 回 `action=already_loaded`，雲端資料與 baseline 一致。
2. **live reset**（`--apply --reset`）會刪除分區內所有文件，**必須先有 #126 的 data owner 明確授權**，
   目前沒有；未授權前不執行。
3. 重設後讀回：`GET /inventory`、`/shopping`、`/forecast` 與 checklist §5 逐項比對。
4. 每次破壞性情境（購買、取消）排練後重設一次再進下一輪（需 #126 授權）。

## 5. 里程碑現況

| 階段 | 內容 | 狀態 |
| --- | --- | --- |
| M0 | 確認部署 owner | ✅ B2 為主要實例 owner，已記錄於 PR #134 與 runbook §7；checklist 檔案需 C2 同步更新 |
| M1 | 容器化 + 雲端上線 | ✅ `/health` 回 200 |
| M2 | 接上 Cosmos + baseline | ✅ `/inventory` 7 筆、`/shopping` 3 筆，baseline `already_loaded` |
| M3 | 接上 OpenAI / Speech / Vision | ✅ OpenAI、Speech；⚠️ 雲端 Vision 無獨立證據（本機已通過） |
| M4 | base URL 交 C2、Android 實機排練 | ⚠️ 已在 PR #134 @ C2；實機排練未回報 |
| M5 | 確認卡流程（`/agent/resume`） | ⚠️ 路由已接上（未知卡回 404）；真實確認 / 取消 / 重試未在雲端實測 |
| M6 | Demo 當天 preflight | ⏳ 未到；開場前 smoke test 由 B1 執行 |
| M7 | B1 備用實例就緒 | ⏳ 未開始 |

## 6. Demo 當天 smoke test（不改任何共用檔）

`python3 deploy/smoke_test.py --base-url <BASE_URL>`，分層檢查，每一項各自回報、不因第一個失敗而停止：
L0 雲端版本是否為最新 `origin/main`；L1 `/health`、真實語音逐字稿、Agent 有回答、`/agent/open-card` 路由格式正確（#215）；
L2 `/inventory`、`/shopping`、`/forecast` 的不變量（不比對固定 baseline）；
L3 拍照 → 辨識 → 確認卡格式正確、拍照本身不改庫存；卡留著不處理，30 分鐘後自己過期。
L4（`--write --yes`）會真的寫入：拍照 → `increase_stock` 卡 → 確認 → 數量 +N，同卡再確認不重複寫入。若同一家庭已有未確認的卡，拍照會回同一張舊卡，L4 就確認那張（先檢查它是單一 `increase_stock`）。
只用標準函式庫，不需憑證、不印金鑰；L0～L3 不寫 domain data（L3 會暫存一張待確認卡，不取消）。不以 mock 補位。

## 7. 風險

| 風險 | 緩解 |
| --- | --- |
| 他人看不到 B2 的 Azure 資源，B2 不在時無人能修 | 備用實例（B1）、runbook §7、公開 URL 驗收；Demo 前凍結雲端 |
| 暫存上傳狀態可能在記憶體（Issue #98），單副本是硬限制 | 副本維持 1；Demo 前不重啟；只在開場前切換實例 |
| pull-only token 密碼 30 天到期（目前到 2026-12-15） | Demo 前確認到期日，必要時重跑 `deploy-aca.sh` 輪替（runbook §6） |
| 兩個實例共用同一個 Cosmos，reset 與購買排練互相影響 | 排練前先協調；reset 需 #126 授權 |
| App 不能換 base URL，備用實例無法真正切換 | 待 C2 回覆；回覆前備用實例只保證能起來並通過 smoke test |
| live reset 尚未授權，排練改動資料後無法還原 | 取得 #126 授權前，排練只做唯讀情境，或接受資料被改動 |
| 冷啟動導致第一個請求逾時 | 副本最小數設 1，不縮到 0 |

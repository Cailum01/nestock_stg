# Nestock Demo 架構與專案大綱

本文依目前專案結構及 Demo readiness 清單整理。標記「未完成｜期望要有」的項目是 Final Demo 預期能力，不代表已完成實作或整合驗收。

## 1. Demo 目標

Final Demo 預期由 Android 實體手機連到雲端 HTTPS API，展示 Nestock 的庫存查詢、低庫存／預測、購物清單、照片辨識與購買確認流程。Demo 的重點不只是畫面可操作，也要能證明使用者確認後的資料確實寫入、重新讀取仍一致，且取消或重試不會造成錯誤資料變更。

## 2. 軟體架構

```text
Flutter App（Android 為主要 Demo 客戶端）
├── Agent Home：對話、文字輸入、照片／語音入口
├── Inventory：庫存檢視
├── Shopping：待購清單檢視
└── Injected Clients：AgentClient、CatalogClient
        │ HTTPS API
        ▼
FastAPI Backend
├── API Routes：健康檢查、庫存、購物清單、預測、Agent、媒體／語音
├── Agent Runtime：意圖協調、工具呼叫、核准流程
├── Tools：將 Agent 查詢轉交共用 Domain Services
├── Domain Services：庫存、購物、預測及交易規則
├── Provider Adapters：Azure OpenAI、Vision、Speech、商品選項
└── Pydantic Models：後端資料與 API 契約來源
        │
        ▼
Cosmos DB
```

### 架構原則

- 一般 API 路由與 Agent Tools 共用同一組 Domain Services，不在不同介面重複實作業務規則。
- Pydantic Models 是後端契約來源；FastAPI 依路由模型產生 OpenAPI。
- 預測資料與已確認庫存分離。預測不得直接覆寫庫存數量。
- 需要持久化變更時，先向使用者提出核准，再由後端驗證並執行；不得將泛用提案內容或用戶端核准布林值直接當成寫入授權。
- 重試同一個已核准操作時，需沿用穩定操作識別，避免重複寫入。
- Agent/HITL 協定、部分服務介面及 Flutter 顯示投影仍屬暫定設計，需由相關 owner 協調後定案。

## 3. 系統架構

### Final Demo 目標部署

```text
Android 實體手機
        │ HTTPS，直接連線
        ▼
雲端 FastAPI API
        ├── Azure OpenAI：Agent 對話與工具協調
        ├── Azure Vision：照片辨識
        ├── Azure Speech：語音轉文字
        └── Cosmos DB：Demo 庫存、購物清單、交易及核准資料
```

Final Demo 不應依賴 `localhost`、`127.0.0.1`、Android Emulator 專用的 `10.0.2.2`、ADB reverse，或由展示者筆電代跑後端。部署負責人及雲端 API 網址仍需確認。Android 實體機是主要展示目標；iOS 實體機測試若無裝置，應明確標示未驗證。

### 主要使用流程

```text
使用者提問／拍照／語音
        ↓
Flutter App 呼叫雲端 API
        ↓
Agent 判斷意圖，呼叫共用服務或外部能力
        ↓
查詢結果直接回覆；需寫入時先建立待確認核准卡
        ↓
使用者確認或取消
        ↓
後端驗證核准並執行操作，回傳結果
        ↓
App 重新讀取並顯示持久化後的資料
```

## 4. 專案模組與狀態

| 模組 | 目前狀態 | Demo 預期 |
|---|---|---|
| Flutter App | 已有 Agent Home、Inventory、Shopping 等骨架與基礎互動測試。 | **未完成｜期望要有：** Android 實體機完整連線雲端 API，並以後端資料呈現 Inventory、Shopping 與 Agent 結果。 |
| FastAPI Backend | 已有 API 骨架、健康檢查、Pydantic 模型、服務邊界及測試。README 列有庫存、購物、預測及 Agent 路由。 | **未完成｜期望要有：** 將 Demo 所需 API 部署至穩定 HTTPS 端點，手機可直接存取。 |
| Agent / HITL | 已有 Agent API 與核准流程基礎；完整的跨端使用者流程仍待驗收。 | **未完成｜期望要有：** 查詢、建立核准卡、確認／取消、重試及最終結果均與 Flutter 契約整合。 |
| Inventory / Shopping | 已有模型與讀取服務邊界。雲端讀取有驗證證據，但 App 與雲端資料一致性尚未完成整合驗收。 | **未完成｜期望要有：** App 與 Agent 使用同一個 Demo household 的資料，確認變更後重新整理仍一致。 |
| Cosmos DB / Persistence | 已有多項讀寫、冪等及測試環境驗證；Demo household 的資料曾做唯讀讀回比對。 | **未完成｜期望要有：** Demo 資料可安全載入、重設及復原，並完成確認寫入後 App 重新讀取的驗收。執行正式資料重設前仍須依資料保護流程進行預檢並取得授權。 |
| Forecast / Low-stock | 已有 Forecast API／服務基礎；演算法與 Demo 結果穩定性仍待驗收。 | **未完成｜期望要有：** 穩定展示「洗衣精快用完」及衛生紙正常庫存等案例；資料不足時明確回傳 insufficient_data，不捏造精確度。 |
| Vision / Media | 已有暫定媒體授權及上傳路由；實際裝置到雲端服務的流程尚未完成 Demo 驗收。 | **未完成｜期望要有：** Android 拍照、上傳、取得 opaque `image_ref`、Azure Vision 辨識、庫存比對及核准卡完整串接。 |
| Speech | 語音轉文字路由目前仍提供 Demo 行為，非完整實際服務流程。 | **未完成｜期望要有：** Android 麥克風輸入、中文轉錄、文字回填對話，以及可恢復的錯誤提示。 |
| Shopping / Purchase Workflow | 有服務與持久化能力基礎；Inventory、Shopping、Transaction 的完整使用者驗收尚未完成。 | **未完成｜期望要有：** 確認實際購買數量後，正確更新庫存、完成購物項目、記錄交易；取消不改業務資料，同一核准重試不重複寫入。 |
| Cloud Deployment / Operations | Demo readiness 清單仍列部署責任、雲端連線與健康檢查為待完成或待確認。 | **未完成｜期望要有：** 確認部署負責人、雲端身分與服務連線，提供 Demo 前可執行的健康檢查及 smoke test。 |
| Demo Data / Acceptance | 已有候選資料基線與驗收清單；Final Demo Script、重設演練及完整跨團隊 E2E 證據尚未完成。 | **未完成｜期望要有：** 凍結展示情境、可重複的基線復原流程及驗收證據；額外旅程需標示 Included 或 Deferred。 |

## 5. 建議 Demo 展示主線

1. **庫存查詢：** 詢問衛生紙數量與位置，回答須符合 Demo 資料。
2. **低庫存／預測：** 展示洗衣精快用完，並以衛生紙展示正常庫存或合理預測。
3. **購物清單：** 確認 App 與 Agent 顯示一致的待購項目。
4. **照片辨識：** 拍攝商品，完成上傳、辨識、比對，並在任何業務寫入前顯示核准卡。
5. **確認購買：** 使用者回報實際購買數量並確認後，更新 Inventory、Shopping 及 Transaction。
6. **取消與重試：** 取消核准不改變業務資料；重送同一核准不產生重複效果。
7. **重新整理：** 確認已核准的變更仍可由後端讀回並顯示。

## 6. Demo Ready 驗收條件

- Android 實體手機可直接連線雲端 HTTPS API，並完成預定展示流程。
- Demo 關鍵能力使用預期的實際服務；模擬結果不得呈現為真實服務成功。
- 使用者核准的持久化變更在 App 刷新或重新開啟後仍正確。
- 所有業務寫入符合目前核准／明確操作規範；取消不寫入，重試不重複寫入。
- Demo 資料可回復到已知基線，並有可查驗的讀回結果。
- 展示者不需在展示期間操作終端機或在筆電啟動後端。
- 部署責任及 Final Demo API 網址已確認。
- 額外旅程與未驗證平台能力已明確標示為 Included、Deferred 或未驗證。

## 7. Owner 協作範圍

| Owner | 主要範圍 |
|---|---|
| A1 | Agent、工具協調、HITL 協定與核准驗證 |
| A2 | Vision、媒體、語音、命名正規化及商品選項服務 |
| B1 | Domain Models、Cosmos 持久化、Inventory 與資料基線 |
| B2 | Transaction History、Forecast、低庫存及部署（部署責任仍須確認） |
| C1 | Shopping 服務、購買流程與 Demo 資料協調 |
| C2 | Flutter 前端、Android 使用者流程及顯示整合 |
| QA | 跨功能驗收規劃、測試案例與品質證據；不擁有產品行為決策 |

跨 Inventory、Shopping、Transaction 的寫入、購買流程原子性、Agent 核准協定及資料基線重設，需由相關 owner 共同確認；本文件不替代這些決策。

## 參考文件

- [專案 README](../README.md)
- [系統架構與決策](./architecture.md)
- [契約說明](../contracts/README.md)
- [Demo readiness checklist](../demo-readiness-checklist-V3.md)
- [驗證紀錄](./validation.md)

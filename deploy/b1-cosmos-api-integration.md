# B2 → B1：雲端 API 與 Cosmos 如何搭配

> 對象：B1。聚焦「雲端部署後，API 如何連 Cosmos、需要 B1 提供什麼」；
> 預測端查詢的細節已併入 §2.1，本文件自成一體。
> 內容依 #96 合併後的 `main`。
> 權威 schema 仍是 `backend/app/models` 與 `/openapi.json`，本文件不是第二份。

## 1. 部署架構

```text
Android ─HTTPS─▶ Azure Container Apps（nestock-api，單一副本）
                    └─▶ Cosmos DB（NoSQL，單一 container，分區鍵 household_id）
```

- 託管：Azure Container Apps（訂閱 3、資源群組 `GAE26530RG`，eastasia）。
- 單一副本：影像暫存上傳（`/staging/*`，A2 的 provisional adapter）目前存在行程記憶體，
  多副本或重啟會遺失，所以最小/最大副本皆為 1。確認卡**不是**這個原因：
  確認卡已由 `ApprovalService` 經 B1 的 Cosmos repository 存進 `household-data`（見 §2）。
- Container App `nestock-api` 以自己的 system-assigned identity 連 Cosmos，
  該 identity 已有資料平面角色（見 §3）。

## 2. 端點與 Cosmos 的關係

| 端點 | 讀寫 | Cosmos 行為 | 備註 |
| --- | --- | --- | --- |
| `GET /inventory` | 讀 | `InventoryItem`（`record_type` 篩選，`household_id` 分區） | 三個 `NESTOCK_COSMOS_*` 皆未設 → 回內建 demo 資料並警告 |
| `GET /shopping` | 讀 | `ShoppingItem` | 同上 |
| `GET /forecast`、`/forecast/reorder`、`/forecast/{item_id}` | 讀 | 庫存 + 整個 household 的 `Transaction` | 多欄排序，需 composite index（§2.1） |
| `POST /agent/messages` | 讀；有 Cosmos 時另存確認卡 | 唯讀工具走同一批 domain service；唯一的卡片工具 `complete_purchase` 只把 `pending_approval` 卡存進 `household-data` 並回傳，不寫領域資料 | 需 Azure OpenAI 設定（`backend/.env`），否則 501；沒有 Cosmos 時不提供卡片工具 |
| `POST /agent/resume` | 寫 | `ApprovalService` 依 `contracts/approval-lifecycle.md` 驗證後，經 `CosmosWriteRepository` 執行已核准的原子批次，冪等 | 已可用；未設定 Cosmos → 503，找不到卡 → 404，選擇不符 → 409 |
| `/api/media/*`、`/api/speech/*`、`/staging/*` | 不碰 Cosmos | — | A2 |

### 2.1 Transaction 查詢與索引

`cosmos_read.py` 的 Transaction 查詢（預測用）：

```sql
SELECT * FROM c
WHERE c.record_type = @record_type AND c.household_id = @household_id
ORDER BY c.created_at DESC, c.transaction_id ASC
```

多欄 `ORDER BY` 必須有**方向相符**的 composite index，單一 `/created_at` 索引不夠，
查詢會失敗。請在 container 的 indexing policy 加入：

| 順序 | path | order |
| --- | --- | --- |
| 1 | `/created_at` | descending |
| 2 | `/transaction_id` | ascending |

（Cosmos 也接受兩個方向都相反的組合，但請以上列為準。）選填的 `since` 過濾
（`c.created_at >= @since`）使用一般 range index。請 B1 **以實際查詢驗證**
（對 `demo-household` 跑上述 SQL，確認不報錯並讀回資料）；若 B1 的 policy 已不同，
請回覆實際的 composite path 與方向，B2 會對齊查詢而不是反過來。

### 2.2 目前狀態

- **已可用**：`GET /inventory`、`/shopping`、`/forecast*` 讀 Cosmos；`POST /agent/messages`
  存購買確認卡；`POST /agent/resume` 執行已核准寫入（皆需 `NESTOCK_COSMOS_*` 與
  Cosmos 資料平面寫入權限）。
- **尚未實作**（`contracts/approval-lifecycle.md`）：變更卡片（§7）、提醒（§8）、
  記錄變動後 Agent 自行產生新卡（§4 第 6 點，目前請使用者重說）。
- **仍為暫存**：`/staging/*` 影像暫存在記憶體（A2）。

啟用規則（`app/core/dependencies.py`）：
**三個變數全設 → 連 Cosmos；全沒設 → demo 資料；只設一部分 → 啟動期報錯。**
Final Demo 必須三個全設，否則會用假資料而不報錯（只有警告），務必以 smoke test 讀回 baseline item_id 驗證。

## 3. 認證（現況已配置）

`services/cosmos_sdk.py` 只用 `DefaultAzureCredential()`，沒有 key 認證，程式不用改。
依 `datalake/azure-database-user-guide.md`（「Container App managed identity」一節），
2026-11-13 起 Container App **`nestock-api` 的 system-assigned identity** 已取得
**Cosmos DB Built-in Data Contributor**，範圍限 `nestock` / `household-data`。
這個角色涵蓋讀取與 `/agent/resume` 的寫入。

| 情況 | 做法 |
| --- | --- |
| **既有 app `nestock-api`** | 不需任何 secret 或額外授權。`deploy-aca.sh` 會重用它的 system-assigned identity，B1 無須再做事 |
| **全新的 app（換名稱或重建）** | system-assigned identity 的 principal ID 會改變，需請 B1 對新 principal 指派同一個角色（範圍同上）。B2 提供 `az containerapp identity show` 的 principalId |

不使用 service principal，也不產生長期 `AZURE_CLIENT_SECRET`。只有在 managed identity
確實不可用時才重新討論（需 B1 決定，因為 key 認證要改共用檔 `cosmos_sdk.py`）。

Image 從 ACR 拉取**永遠不啟用 ACR admin user**，依 `deploy-aca.sh` 的判斷分兩條路：

1. 首選：同一個 identity 只授予 **AcrPull**。
2. 本專案（學生角色）：角色有 ABAC 條件，只能把角色指派給 User，不能指派給
   ServicePrincipal，`roleAssignments/write` 會回 `AuthorizationFailed`，課程方管理員也無法代為
   指派。此時腳本自動改用 **pull-only ACR token** `nestock-api-pull`：scope 僅
   `repositories/nestock-api/content/read` 與 `metadata/read`（不能推送、不能讀其他
   repository），密碼 30 天到期、可撤銷，只存在 Container App 的 registry 設定。
   輪替與撤銷步驟見 `deploy/demo-day-runbook.md` 的「ACR pull token」一節。

其他類型的 role assignment 錯誤（非 `already exists`、非授權拒絕）腳本仍會直接停止。

## 4. B1 需交付給 B2 的清單

| # | 項目 | 用途 |
| --- | --- | --- |
| 1 | `NESTOCK_COSMOS_ENDPOINT` | 例：`https://<account>.documents.azure.com:443/` |
| 2 | `NESTOCK_COSMOS_DATABASE` | database 名稱 |
| 3 | `NESTOCK_COSMOS_CONTAINER` | container 名稱（目前理解為 `household-data`） |
| 4 | 既有 `nestock-api` 已完成（§3）；僅在**重建 app** 時，對新 identity 重新指派 Data Contributor | 雲端連線 |
| 5 | composite index `/created_at` desc + `/transaction_id` asc 已建立，並以實際查詢驗證（§2.1） | 預測查詢 |
| 6 | Cosmos 防火牆：允許來源 | 若帳號啟用 IP 限制，需放行 Container App 的出站 IP（B2 部署後提供） |
| 7 | Demo 重設方式（清除 `demo-household` 分區、approval、operation receipt） | checklist §5 |
| 8 | baseline item_id 採 `seed-*`（checklist §5） | 種子腳本預設是名稱雜湊 `shopee-<hash>` |

## 5. B2 部署後會提供給 B1

- 公開 base URL：`https://<app>.<region>.azurecontainerapps.io`
- Container App 出站 IP（供 Cosmos 防火牆）。
- 驗證步驟（B1 可自行執行）：

```bash
curl -s https://<base-url>/health
curl -s https://<base-url>/inventory | jq '.[].item_id'      # 應為 7 個 seed-* id
curl -s https://<base-url>/shopping  | jq '.[] | {item_id,status}'
curl -s https://<base-url>/forecast  | jq '.[] | {item_id,status,suggested_action}'
```

## 6. 注意事項

- 所有查詢都帶 `household_id`；Demo 固定 `demo-household`。
- 預測永不寫入 `InventoryItem.quantity`。
- 目前 `/health` 不檢查 Cosmos，因此「`/health` 通」不代表 Cosmos 連得上，
  請以 `/inventory` 讀回資料為準。

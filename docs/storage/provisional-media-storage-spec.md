# Provisional Storage Specification (Owner: A2)

[SCOPE: MEDIA_STORAGE_PORT_AND_ADAPTER | ZERO_PRICE_PERSISTENCE]

本規格文件定義 Nestock A2 多模態感知之暫存媒體儲存架構（Provisional Media Storage Architecture）、實體分工、雙模式轉接器運作機制、與 Cosmos DB 中繼資料映射生命週期契約。

---

## 1. 系統架構與第一性原理 (Architecture & First Principles)

依據專案核心憲法與領域職責邊界：
1. **二進位與結構化分離原則**：圖片原始二進位位元組（Raw Binary Bytes）**絕對禁止存入 Cosmos DB 或任何關聯/文件資料庫**。二進位資料必須且只能存在物件儲存空間（Azure Blob Storage 或本地記憶體隔離緩衝區）。
2. **零價格持久化原則**：感知解析出之商品或購買選項，嚴禁持久化單價或價格資訊。
3. **分區隔離鐵律**：Cosmos DB 與 Azure Blob Storage 皆以 `household_id` 作為物理分區與路徑階層（`households/{household_id}/media/{input_id}.jpg`），嚴禁跨家庭資料洩漏。
4. **Agent 門禁邊界 (completed-only)**：遵循 Issue #98 與設計文件 §8.2，**只有處於 `COMPLETED` 狀態之媒體映射可供 Agent 使用**；未就緒之 `ALLOCATED`、`RECEIVED`、`FAILED` 或跨家庭查閱一律嚴格阻斷。
5. **六大擁有者權限邊界 (Owner A2)**：
   - 負責路徑：`backend/app/services/provisional_storage/`、視覺感知適配器、測試套件。
   - 不越界碰觸 B1 實體商品存儲或 C1 採購流程。

```
[Flutter Mobile Client]
       │
       │ 1. POST /api/media/upload-authorization (filename, content_type, size)
       ▼
[FastAPI Backend Router]
       │
       │ 2. MediaStorageService.create_upload_authorization()
       ├─────────────────────────────────┐
       ▼                                 ▼
[Cosmos DB (household-data)]     [Token Cache (5 min TTL)]
Status: ALLOCATED (TTL: 24h)     staging-token-{input_id}
       │
       │ 3. Client direct PUT /staging/upload/{upload_token}
       ▼
[FastAPI Staging Endpoint]
       │
       │ 4. MediaStorageService.receive_staged_upload()
       ├─── Validate size & sha256 (fail_processing on error)
       ├─────────────────────────────────┬─────────────────────────────────┐
       ▼                                 ▼                                 ▼
[Azure Blob / Local Staging]     [Cosmos DB]                     [Token Eviction]
media container                  Status: RECEIVED                Token invalidated
households/{hh}/media/{id}.jpg   file_size_bytes, sha256_hash
       │
       │ 5. Ingest Processing Pipeline (auto_process=True)
       ├─────────────────────────────────┬─────────────────────────────────┐
       ▼                                 ▼                                 ▼
[Azure OpenAI GPT-4o Vision]     [Azure Blob / Local]            [Cosmos DB]
Infer items & OCR text           .../derived/vision_v1.json      Status: COMPLETED (TTL: 24h)
                                                                 (Agent Gate Unlocked)
```

---

## 2. 雙模式架構與相依性倒置 (Dual-Mode Hexagonal Architecture)

系統透過相依性倒置原則 (Dependency Inversion Principle, DIP) 定義抽象接口，讓業務邏輯完全解耦於底層儲存媒介：

### 2.1 介面契約 (`interfaces.py`)
- `MediaStorageService`:
  - `create_upload_authorization(request, base_staging_url)`: 分配 `input_id`、產出簽章/Token URL、Cosmos 註冊 `ALLOCATED` 狀態。
  - `receive_staged_upload(upload_token, payload_bytes)`: 接收二進位串流、寫入儲存體、驗證 SHA256 與大小、Cosmos 推進至 `RECEIVED`；當 `auto_process=True` 時執行 Ingest 視覺分析並將衍生工件 `vision_v1.json` 落盤後方可推進至 `COMPLETED`。
  - `begin_processing(household_id, image_ref)`: 推進至 `PROCESSING` 狀態，具備 COMPLETED 等冪保護。
  - `complete_processing(household_id, image_ref, artifact_data)`: 強制要求非空 `artifact_data`，先將衍生分析工件持久化至儲存體，成功後方可推進至 `COMPLETED` 狀態。
  - `fail_processing(household_id, image_ref, failure_reason)`: 推進至 `FAILED` 狀態並記錄失敗原因。
  - `verify_media_exists(household_id, image_ref)`: Agent 門禁守衛，調用 `find_completed_by_image_ref`，僅放行 COMPLETED（`AzureBlobMediaStorage` 於缺少 `mapping_port` 時強制 Fail-Closed 回傳 `False`），非預期資料庫異常嚴格向上傳播。
  - `get_media_bytes(household_id, image_ref)`: 提供唯讀下載原始二進位位元組串流。
  - `get_derived_artifact(household_id, image_ref)`: 提供唯讀下載已持久化之 `vision_v1.json` 衍生工件位元組，供 `VisionService` 直接消費。
  - `retain_media(household_id, image_ref)`: 將媒體與衍生工件標記為業務附加 (`attached`)，豁免 24h 孤立清理。
  - `unretain_media(household_id, image_ref, ttl_seconds=86400)`: 當下游解除附件或替換既有圖片時，將標記還原為孤立狀態 (`orphan`) 並重新配置 24h 過期時間，以利底層雲端政策回收。
  - `cleanup_expired_blobs(now)`: 掃描並刪除逾 24h 且未被業務附加保留 (`ttl_seconds=None`) 之孤立原始媒體與衍生工件。
  - `get_media_snapshot(household_id, image_ref)`: 提供下游業務附加 (PR #105) 進行原子保留與 ETag 校驗之 Snapshot。

### 2.2 轉接器實作 (Adapters)
1. **`AzureBlobMediaStorage` (`azure_blob_adapter.py`) - [PROD_CLOUD]**：
   - 連線策略：優先支援 `AZURE_STORAGE_CONNECTION_STRING`，次選 `AZURE_STORAGE_BLOB_ENDPOINT` 搭配 `DefaultAzureCredential`（Azure CLI / 受控識別 Managed Identity）。
   - 容器名稱：預設 `media`。
   - 雲端路徑規則：`households/{household_id}/media/{input_id}.jpg`。
   - 衍生工件規則：`households/{household_id}/media/{input_id}/derived/{artifact_name}`。
   - 安全規範：內建 10MB 大小熔斷、逾時防護、例外不吞噬傳播、COMPLETED 等冪保護與樂觀並發 ETag 重試防護。
   - 支援邊界 (Support Boundary)：採用目錄前綴物理隔離架構（Prefix-based Lifecycle Isolation）。孤兒暫存媒體存於 `staging/` 前綴，業務附著媒體原子遷移至 `attached/` 前綴。全環境原生相容階層命名空間 (Hierarchical Namespace, HNS / ADLS Gen2) 帳戶（如 `stnestockdev20261101`）與標準 GPv2 帳戶，徹底拔除對 Blob Index Tags 之依賴，以 Azure 原生 Lifecycle 前綴排程確定性保障 24h 孤兒清理與附著媒體永久保留（註：業務附件在儲存端之永久保留需待 Issue #98 §4 之流程接線完成方告生效，詳見 §4.1 限制聲明）。
2. **`LocalStagingMediaStorage` (`local_staging_adapter.py`) - [DEV_FALLBACK]**：
   - 記憶體字典快取，支援無雲端環境或離線單元測試。
   - 介面、狀態轉移與防禦行為 100% 保持與 Azure Blob Storage 對齊。

### 2.3 動態雙模式工廠 (`dependencies.py`)
- 後端啟動時透過 `_create_media_storage_service()` 進行自動偵測（三態防禦律）：
  - 若偵測到 Azure Blob 環境變數且連線成功 -> 動態實例化 `AzureBlobMediaStorage`。
  - 若配置了 Azure Blob 環境變數但連線失敗或配置無效 -> 強制拋出 `RuntimeError` 阻斷啟動（Fail-Fast / Fail-Honest），嚴禁靜默降級為記憶體型 `LocalStagingMediaStorage`，徹底杜絕相片假持久化與靜默遺失風險。
  - 僅在環境完全未配置任何雲端環境變數時 -> 合規 Fallback 至 `LocalStagingMediaStorage` 作為開發與離線測試用途。

---

## 3. Cosmos DB 映射生命週期契約 (`media_mapping.py`)

結構化中繼資料以五態狀態機 (Five-State Lifecycle) 與 ETag 樂觀鎖維護：

| 狀態 (Status) | 觸發時機 | 欄位變更 | 孤兒清理 TTL | Agent 可用性 |
| :--- | :--- | :--- | :--- | :--- |
| `ALLOCATED` | 用戶端請求上傳授權 | 產出 `input_id`、`upload_authorization_expires_at` | 86,400 秒 (24h) | 阻斷 (`PHOTO_NOT_FOUND`) |
| `RECEIVED` | 二進位資料成功寫入 Storage/Blob | 填入 `sha256_hash`、`file_size_bytes`、更新時間 | 86,400 秒 (24h) | 阻斷 (`PHOTO_NOT_FOUND`) |
| `PROCESSING` | 背景/Ingest Vision 模組取走圖片 | 推進至 PROCESSING、更新時間 | 86,400 秒 (24h) | 阻斷 (`PHOTO_NOT_FOUND`) |
| `COMPLETED` | 視覺推論完成並產出衍生工件 | 填入 `artifact_ref`、更新時間（維持孤立 TTL） | 86,400 秒 (24h)＊ | 放行 (`verify_media_exists=True`) |
| `FAILED` | 逾時、大小超標或辨識異常 | 填入 `failure_reason` (UPLOAD_TIMEOUT, etc.) | 86,400 秒 (24h) | 阻斷 (`PHOTO_NOT_FOUND`) |

＊**重要備註**：`COMPLETED` 僅代表「抽取/處理完成」，未被確認之業務紀錄附加前**仍屬於孤立媒體，維持 24 小時孤兒清理 TTL**。只有當 C1/B1 於確認購買或庫存記錄附加該媒體時（PR #105），始於交易批次中將 TTL 清空以永久保留。

---

## 4. 關鍵環境變數與安全設定

```ini
# Cosmos DB
# 或使用連線字串
# AZURE_STORAGE_CONNECTION_STRING="DefaultEndpointsProtocol=https;AccountName=...;AccountKey=...;EndpointSuffix=core.windows.net"
```

### 4.1 生產環境 Azure Blob Storage 24h 孤兒清理生命週期政策 (Lifecycle Management)

依據設計文件 §8.2 與 Issue #98 驗收標準，生產端 Azure Blob 容器（`media`）配置原生 Lifecycle Management 規則，將未附加業務紀錄的孤立檔案於 24 小時後自動 Purge。

本架構全面採用 **路徑前綴物理隔離 (Prefix-based Lifecycle)**，政策精確匹配 `prefixMatch: ["media/staging/"]`，已附加於業務紀錄之檔案移入 `media/attached/` 天然豁免於清理規則，不依賴任何 Blob Index Tags，原生相容 HNS (ADLS Gen2) 與一般 Blob 儲存體。

正式部署定義檔已獨立落盤於 [`deploy/azure-blob-lifecycle-policy.json`](../../deploy/azure-blob-lifecycle-policy.json)：

```json
{
  "rules": [
    {
      "enabled": true,
      "name": "purge-staging-orphan-media-after-24h",
      "type": "Lifecycle",
      "definition": {
        "filters": {
          "blobTypes": ["blockBlob"],
          "prefixMatch": ["media/staging/"]
        },
        "actions": {
          "baseBlob": {
            "delete": {
              "daysAfterCreationGreaterThan": 1
            }
          }
        }
      }
    }
  ]
}
```

#### Azure CLI 套用指令 (ARM 管理層級)
雲端資源管理者可透過標準 Azure CLI 指令將本政策套用至目標儲存帳戶：
```bash
az storage account management-policy create \
  --account-name <STORAGE_ACCOUNT_NAME> \
  --resource-group <RESOURCE_GROUP> \
  --policy @deploy/azure-blob-lifecycle-policy.json
```

＊**保留與解除保留語義契約**：
1. **上傳預設孤立**：上傳時原始圖檔與衍生工件預設寫入暫存前綴（儲存帳戶層級：`media/staging/households/{hid}/...`；容器相對路徑：`staging/households/{hid}/...`）。
2. **附著保留 (`retain_media`)**：當下游業務（如購物完成或庫存確認）持久化時，將原始圖與衍生工件原子遷移至保留前綴（儲存帳戶層級：`media/attached/households/{hid}/...`；容器相對路徑：`attached/households/{hid}/...`），因脫離 `staging/` 前綴，永久排除於 Lifecycle 自動清理之外。
3. **解除保留與寬限期 (`unretain_media`)**：當商品刪除或換圖解除附件時，適配器將檔案移回 `staging/` 前綴並執行時間戳刷新（重設 `CreationTime`），使 Azure 原生排程之 `daysAfterCreationGreaterThan: 1` 重新自該時刻起算，保證享有完整的 24 小時寬限期。
4. **階層命名空間 (HNS) 原生相容**：無論儲存帳戶是否啟用階層命名空間 (HNS / ADLS Gen2)，前綴清理規則均 100% 確定生效，完全不需要 Blob Index Tags，且服務端無需維運任何刪除排程器，完全滿足 §8.2 基礎架構清理不變量。
5. **政策測試模擬說明**：單元測試中的 `simulate_azure_lifecycle_policy_purge` 僅為本地對 Azure Lifecycle 前綴過濾與時間差的 Mock 邏輯模擬驗證；生產環境之生命週期清理完全由雲端 Azure Storage Lifecycle Management 原生政策引擎執行，應用服務端零刪除排程與背景定時迴圈。
6. **PR #155 邊界與業務附件保護限制聲明 (Scope Boundary & Business Attachment Caveat)**：
   - 本規格書與 PR #155 之儲存適配器實作（`AzureBlobMediaStorage` / `BaseMediaStorageLifecycle`）已完備 `retain_media`、`unretain_media`、實體時間戳刷新（寬限期重算）與跨實例／重啟時向 Cosmos DB 查詢還原上下文（Stateless Recovery）之底層儲存能力。
   - **明確限制本 PR 不宣稱真實業務附件受到端到端保護**：目前 C1 購物核准流程（`ShoppingWorkflowBuilder`）僅在 Cosmos 交易批次中清除 Mapping 之 TTL，尚未接線呼叫 `retain_media`（即批次提交後缺少 Post-Commit Handoff 呼叫）。在端到端真實工作流中，已確認商品之照片實體仍停留在 `staging/` 前綴下，受限於雲端 Lifecycle 政策。
   - **依賴與接續規劃**：依據 Issue #98 之先後順序，端到端之 Post-Commit Handoff、重試佇列與真實 Approval Workflow 整合驗證，明確界定由後續之「步驟 4：C1、A2 完成購買流程接線」專屬處理。本 PR 嚴格聚焦於 A2 儲存適配器之能力交付與規格對齊，不做超出當前實現範圍之端到端宣稱。

---

### 4.2 業務保留與重試契約 (Retention & Retry Contract)

依據 Issue #98 第 1 步「對齊 mapping／`retain_media` 契約及重試」之要求，明確規範調用端與儲存層之契約行為：

#### 4.2.1 觸發時機與職責分工 (Post-Commit Handoff)
- **觸發順序**：業務寫入必須堅持「資料庫事務優先」原則。下游業務（如 C1 購物審批、庫存入庫）在完成 Cosmos DB 交易批次提交（即原子清除 Mapping 之 `ttl_seconds` 並記錄附件關聯）後，方可非同步或同步呼叫 `retain_media(household_id: str, image_ref: str) -> bool`。
- **參數約束 (Parameter Invariant)**：呼叫端傳入之參數為 `image_ref`（如 `blob://households/{hid}/media/{input_id}.jpg`，由上傳結果或 Mapping 記錄之 `image_ref` 欄位取得），嚴禁將純 UUID 之 `input_id` 當作路徑傳入，確保儲存層 `_parse_blob_path` 順暢解析。
- **故障隔離**：若 `retain_media` 執行失敗，不得回滾已提交之業務交易紀錄；應交由重試機制完成 Blob 前綴遷移。

#### 4.2.2 儲存端冪等性保證 (Storage Idempotency Contract)
- **重複執行安全**：`AzureBlobMediaStorage.retain_media` 具備嚴格冪等性保證。若目標 Blob 已經遷移至 `attached/` 前綴（例如先前呼叫超時但實際已成功）：
  - 適配器自動偵測存在狀態，安全略過重複搬移動作；
  - 自動確保其 Metadata 與 ContentSettings 處於正確保留狀態；
  - 絕不拋出 `409 Conflict`、`ResourceExistsError` 或中斷業務流程。

#### 4.2.3 呼叫端重試機制與補償邊界 (Retry Policy & Reconciliation Boundary)
- **暫態故障重試**：當呼叫 `retain_media` 遭遇暫時性網路抖動（如 HTTP 503, 504 或連線超時）時，呼叫端**必須**採用帶有抖動的指數退避重試（Exponential Backoff with Jitter）：
  - **最大重試次數**：3 次。
  - **建議間隔**：第 1 次 1.0 秒、第 2 次 2.0 秒、第 3 次 4.0 秒。
- **重試耗盡降級與補償邊界 [NOT_IMPLEMENTED_IN_PR_155]**：
  - 若連續重試 3 次均失敗，呼叫端**必須**記錄包含 `household_id` 與 `image_ref` 之 `[CRITICAL_MEDIA_RETENTION_FAILED]` 結構化錯誤日誌以待人工干預與警報處理。
  - **能力範圍誠實揭露**：PR #155 階段**尚未實作**自動化背景對齊掃描器（Background Reconciliation Sweeper）、重試發布佇列（Outbox/Retry Queue）或應用程式重啟自 mapping 自動搬移 Blob 之機制。雖然 Cosmos DB Mapping 已設定為永久保留（`ttl_seconds is None`），但 Blob 實體仍停留於 `staging/` 前綴下，在未經補償搬移前，仍存在 24 小時後遭 Azure 原生政策清除之風險。
  - **接續工程指引**：自動化重試佇列、發布者補償與端到端對齊機制，依據 Issue #98 之職能分工，明確排定於後續「步驟 4：C1、A2 完成購買流程接線」中實作，不作為本 PR 之驗收承諾。

#### 4.2.4 雙端時鐘與 TTL 一致性
- 當執行 `unretain_media(household_id: str, image_ref: str, ttl_seconds: int = 86400) -> bool`（例如商品刪除或換圖）：
  1. **Blob 實體層**：檔案遷回 `staging/`，並執行 Self-Overwrite 重設 `CreationTime`，確保享有完整的 24 小時（86400 秒）寬限期。
  2. **Cosmos Mapping 層**：儲存適配器同步透過 `MediaMappingPort.update_retention` 將 TTL 精確重設為 `86400` 秒。
  - 確保雙端之過期淘汰時鐘完全同步，絕不產生幽靈孤兒紀錄。

#### 4.2.5 雲端基礎架構權限邊界與政策套用指引 (Infrastructure Control-Plane Boundary)
- **應用端憑證 (Data Plane)**：應用程式服務僅持有具備容器存取權限之 SAS 連線字串（Data Plane），無權直接調用 ARM 控制台 API 修改 Storage Account 全域政策。
- **客觀 RBAC 權限邊界現況**：
  開發者帳戶（`26511@tibame.com`）執行 Azure CLI 對共享資源群組 `GAE26512RG` 查詢或管理儲存帳戶時，遭遇客觀授權阻斷：
  ```text
  ERROR: (AuthorizationFailed) The client 'mail#26511@tibame.com' does not have authorization to perform action 'Microsoft.Storage/storageAccounts/read' over scope '/subscriptions/186e9d87-a1a6-4dcd-8033-005369690f26/resourceGroups/GAE26512RG/providers/Microsoft.Storage/storageAccounts/stnestockdev20261101'
  ```
  因此，A2 開發端在物理環境上無權限在目標帳戶執行 ARM 政策套用或讀回，此操作屬於訂閱管理者 / B1 (`Cailum01`) 之 Control Plane 職責。
- **正式政策套用與讀回驗收指令 (Control Plane)**：
  請持有 ARM 寫入權限之管理員執行以下標準指令完成部署與驗收：
  ```bash
  # 1. 套用 Lifecycle 政策
  az storage account management-policy create \
    --account-name stnestockdev20261101 \
    --resource-group GAE26512RG \
    --policy @deploy/azure-blob-lifecycle-policy.json

  # 2. 讀回政策以驗收配置生效
  az storage account management-policy show \
    --account-name stnestockdev20261101 \
    --resource-group GAE26512RG
  ```
---

## 5. 實體雲端驗證紀錄 (Physical Verification Audit)

- **測試裝置**: Google Pixel 8a (`42211JEKB11467`)
- **服務端點**: `http://192.168.18.15:8000` (FastAPI + AzureBlobMediaStorage)
- **實測前綴拓撲 (Container: media)**:
  - 暫存上傳 (Orphan): `staging/households/demo-household/media/{input_id}.jpg`
  - 衍生工件 (Derived): `staging/households/demo-household/media/{input_id}/derived/vision_v1.json`
  - 業務保留 (Attached): `attached/households/demo-household/media/{input_id}.jpg`
- **實測寫入紀錄 (Container: media)**:
  1. `b25502d5eec545ff8bed5e48a188c9ee.jpg` (700,779 bytes) - 辨識物: 有線滑鼠
  2. `87dded756a35440a90b4327f616200b1.jpg` (772,487 bytes) - 辨識物: wooden and plastic clip
  3. `5a00873e537040188d003205061dc1fc.jpg` (798,156 bytes) - 辨識物: wooden clip with white plastic tip
  4. `6300435499d14bcda5fe9517ef7df80b.jpg` (777,868 bytes) - 辨識物: USB 3.0 drive (OCR: `hp USB 3.0 27TB`)
- **測試套件狀態**:
  - `uv run pytest tests/test_azure_blob_storage.py`: 23 passed in 1.26s
  - `uv run pytest tests/test_a2_media_pipeline.py`: 14 passed in 3.10s
  - `uv run pytest -k "media or blob or vision"`: 125 passed in 12.25s



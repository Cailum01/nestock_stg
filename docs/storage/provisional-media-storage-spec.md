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

依據設計文件 §8.2 與 Issue #98 驗收標準，生產端 Azure Blob 容器（`media`）需配置原生 Lifecycle Management 規則，將未附加業務紀錄的孤立檔案於 24 小時後自動 Purge：

```json
{
  "rules": [
    {
      "enabled": true,
      "name": "purge-orphan-media-after-24h",
      "type": "Lifecycle",
      "definition": {
        "filters": {
          "blobTypes": ["blockBlob"],
          "prefixMatch": ["households/"]
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
＊**保留例外機制**：已附加業務紀錄之檔案經由 `retain_media` 打上 `retention="attached"` 元資料標籤，於生產環境可配合 Azure Blob Index Tags 或後續 Blob 搬移歸檔至永久保存路徑（`households/{household_id}/permanent/`），避免遭孤兒規則自動清除。當下游執行 `unretain_media` 時，元資料還原為 `retention="orphan"`，恢復孤立回收排程。
---

## 5. 實體雲端驗證紀錄 (Physical Verification Audit)

- **測試裝置**: Google Pixel 8a (`42211JEKB11467`)
- **服務端點**: `http://192.168.18.15:8000` (FastAPI + AzureBlobMediaStorage)
- **實測寫入紀錄 (Container: media)**:
  1. `b25502d5eec545ff8bed5e48a188c9ee.jpg` (700,779 bytes) - 辨識物: 有線滑鼠
  2. `87dded756a35440a90b4327f616200b1.jpg` (772,487 bytes) - 辨識物: wooden and plastic clip
  3. `5a00873e537040188d003205061dc1fc.jpg` (798,156 bytes) - 辨識物: wooden clip with white plastic tip
  4. `6300435499d14bcda5fe9517ef7df80b.jpg` (777,868 bytes) - 辨識物: USB 3.0 drive (OCR: `hp USB 3.0 27TB`)
- **測試套件狀態**:
  - `uv run pytest tests/test_azure_blob_storage.py`: 11 passed in 1.12s
  - `uv run pytest tests/test_a2_media_pipeline.py`: 12 passed in 3.09s
  - `uv run pytest`: 555 passed in 12.51s


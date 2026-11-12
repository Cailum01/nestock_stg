# Provisional Storage Specification (Owner: A2)

[SCOPE: MEDIA_STORAGE_PORT_AND_ADAPTER | ZERO_PRICE_PERSISTENCE]

本規格文件定義 Nestock A2 多模態感知之暫存媒體儲存架構（Provisional Media Storage Architecture）、實體分工、雙模式轉接器運作機制、與 Cosmos DB 中繼資料映射生命週期契約。

---

## 1. 系統架構與第一性原理 (Architecture & First Principles)

依據專案核心憲法與領域職責邊界：
1. **二進位與結構化分離原則**：圖片原始二進位位元組（Raw Binary Bytes）**絕對禁止存入 Cosmos DB 或任何關聯/文件資料庫**。二進位資料必須且只能存在物件儲存空間（Azure Blob Storage 或本地記憶體隔離緩衝區）。
2. **零價格持久化原則**：感知解析出之商品或購買選項，嚴禁持久化單價或價格資訊。
3. **分區隔離鐵律**：Cosmos DB 與 Azure Blob Storage 皆以 `household_id` 作為物理分區與路徑階層（`households/{household_id}/media/{input_id}.jpg`），嚴禁跨家庭資料洩漏。
4. **六大擁有者權限邊界 (Owner A2)**：
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
Status: ALLOCATED                staging-token-{input_id}
       │
       │ 3. Client direct PUT /staging/upload/{upload_token}
       ▼
[FastAPI Staging Endpoint]
       │
       │ 4. MediaStorageService.receive_staged_upload()
       ├─── Validate size & sha256
       ├─────────────────────────────────┬─────────────────────────────────┐
       ▼                                 ▼                                 ▼
[Azure Blob Storage]              [Cosmos DB]                     [Token Eviction]
media container                   Status: RECEIVED                Token invalidated
households/{hh}/media/{id}.jpg    file_size_bytes, sha256_hash
       │
       │ 5. Background Vision Pipeline (AzureOpenAIVisionService)
       ├─────────────────────────────────┬─────────────────────────────────┐
       ▼                                 ▼                                 ▼
[Azure OpenAI GPT-4o Vision]      [Azure Blob Storage]            [Cosmos DB]
Infer items & OCR text            .../derived/vision_v1.json      Status: COMPLETED
```

---

## 2. 雙模式架構與相依性倒置 (Dual-Mode Hexagonal Architecture)

系統透過相依性倒置原則 (Dependency Inversion Principle, DIP) 定義抽象接口，讓業務邏輯完全解耦於底層儲存媒介：

### 2.1 介面契約 (`interfaces.py`)
- `MediaStorageService`:
  - `create_upload_authorization(request, base_staging_url)`: 分配 `input_id`、產出簽章/Token URL、Cosmos 註冊 `ALLOCATED` 狀態。
  - `receive_staged_upload(upload_token, payload_bytes)`: 接收二進位串流、寫入儲存體、驗證 SHA256 與大小、Cosmos 推進至 `RECEIVED`。
  - `store_derived_artifact(household_id, image_ref, artifact_name, artifact_bytes, content_type)`: 寫入視覺推論衍生 JSON 工件。
  - `load_derived_artifact(household_id, image_ref, artifact_name)`: 讀取衍生工件。
  - `get_media_stream(image_ref)`: 提供唯讀下載二進位位元組串流。

### 2.2 轉接器實作 (Adapters)
1. **`AzureBlobMediaStorage` (`azure_blob_adapter.py`) - [PROD_CLOUD]**：
   - 連線策略：優先支援 `AZURE_STORAGE_CONNECTION_STRING`，次選 `AZURE_STORAGE_BLOB_ENDPOINT` 搭配 `DefaultAzureCredential`（Azure CLI / 受控識別 Managed Identity）。
   - 容器名稱：預設 `media`。
   - 雲端路徑規則：`households/{household_id}/media/{input_id}.jpg`。
   - 衍生工件規則：`households/{household_id}/media/{input_id}/derived/{artifact_name}`。
   - 安全規範：內建 10MB 大小熔斷、逾時防護、樂觀並發 ETag 重試防護。
2. **`LocalStagingMediaStorage` (`local_staging_adapter.py`) - [DEV_FALLBACK]**：
   - 記憶體字典快取，支援無雲端環境或離線單元測試。
   - 介面與行為 100% 保持與 Azure Blob Storage 對齊。

### 2.3 動態雙模式工廠 (`dependencies.py`)
- 後端啟動時透過 `_create_media_storage_service()` 進行自動偵測：
  - 若偵測到 Azure Blob 環境變數且連線成功 -> 動態實例化 `AzureBlobMediaStorage`。
  - 若無環境變數或連線失敗 -> 自動平滑降級為 `LocalStagingMediaStorage`，發出 Warning 日誌，不阻斷應用程式啟動。

---

## 3. Cosmos DB 映射生命週期契約 (`media_mapping.py`)

結構化中繼資料以五態狀態機 (Five-State Lifecycle) 與 ETag 樂觀鎖維護：

| 狀態 (Status) | 觸發時機 | 欄位變更 | 孤兒清理 TTL |
| :--- | :--- | :--- | :--- |
| `ALLOCATED` | 用戶端請求上傳授權 | 產出 `input_id`、`upload_authorization_expires_at` | 86,400 秒 (24h) |
| `RECEIVED` | 二進位資料成功寫入 Blob | 填入 `sha256_hash`、`file_size_bytes`、更新時間 | 86,400 秒 (24h) |
| `PROCESSING` | 背景 Vision 模組取走圖片 | 填入 `processing_started_at` | 86,400 秒 (24h) |
| `COMPLETED` | 視覺推論完成並寫入衍生工件 | 填入 `completed_at`、清空 TTL（永久保留） | -1 (永不刪除) |
| `FAILED` | 逾時、大小超標或辨識異常 | 填入 `failure_reason` (UPLOAD_TIMEOUT, etc.) | 86,400 秒 (24h) |

---

## 4. 關鍵環境變數與安全設定

```ini
# Cosmos DB
# 或使用連線字串
# AZURE_STORAGE_CONNECTION_STRING="DefaultEndpointsProtocol=https;AccountName=...;AccountKey=...;EndpointSuffix=core.windows.net"
```

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
  - `uv run pytest tests/test_azure_blob_storage.py`: 7 passed in 0.86s
  - `uv run pytest tests/test_a2_media_pipeline.py`: 9 passed in 1.89s
  - `uv run pytest`: 548 passed in 11.49s

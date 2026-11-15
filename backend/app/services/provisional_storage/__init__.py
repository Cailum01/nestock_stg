"""[PROVISIONAL_STAGING] 過渡期暫存儲存模組 (Provisional Media Storage).

本目錄檔案為在正式 Azure Blob Storage 與 Cosmos DB 資料庫建立前之過渡期實作。
提供本機測試、合約對齊與影像上傳授權生命週期模擬。
"""

from app.services.provisional_storage.azure_blob_adapter import (
    AzureBlobMediaStorage,
)
from app.services.provisional_storage.interfaces import MediaStorageService
from app.services.provisional_storage.local_staging_adapter import (
    LocalStagingMediaStorage,
)

__all__ = ["MediaStorageService", "AzureBlobMediaStorage", "LocalStagingMediaStorage"]

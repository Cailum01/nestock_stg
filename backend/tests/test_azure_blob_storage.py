"""[UNIT_TEST] Azure Blob 媒體儲存適配器單元與生命週期測試.

涵蓋場景：
1. 上傳授權與 Cosmos ALLOCATED 登記
2. 實體二進位位元組寫入 Blob 與 Cosmos RECEIVED 推進 (含 SHA-256 與檔案大小)
3. 取得媒體二進位位元組 (含家庭邊界保護)
4. 視覺衍生工件 JSON 寫入 Blob 與 Cosmos COMPLETED 推進
5. 上傳逾期 (300s) 轉移至 FAILED (UPLOAD_TIMEOUT)
6. 檔案大小不合法轉移至 FAILED (VALIDATION_FAILED)
7. 工廠函式 (_create_media_storage_service) 雙模動態切換驗證
"""

import hashlib
import time

import pytest

from app.core.dependencies import _create_media_storage_service
from app.models.domain import (
    MediaFailureReason,
    MediaProcessingStatus,
)
from app.models.storage import (
    AllowedMediaContentType,
    UploadAuthorizationRequest,
)
from app.services.cosmos_read import CosmosReadRepository
from app.services.cosmos_write import (
    BatchOperationConflict,
    CreateDocument,
    ReplaceDocument,
)
from app.services.media_mapping import CosmosMediaMappingStorage
from app.services.provisional_storage.azure_blob_adapter import AzureBlobMediaStorage
from app.services.provisional_storage.local_staging_adapter import LocalStagingMediaStorage


class FakeBlobClient:
    def __init__(self, container_dict: dict[str, bytes], blob_name: str) -> None:
        self._container_dict = container_dict
        self._blob_name = blob_name

    async def upload_blob(self, data: bytes | str, overwrite: bool = True, **kwargs) -> None:
        payload = data.encode("utf-8") if isinstance(data, str) else data
        self._container_dict[self._blob_name] = payload

    async def download_blob(self):
        if self._blob_name not in self._container_dict:
            raise KeyError(f"Blob {self._blob_name} does not exist.")
        data = self._container_dict[self._blob_name]

        class DownloadStream:
            def __init__(self, content: bytes):
                self._content = content

            async def readall(self) -> bytes:
                return self._content

        return DownloadStream(data)

    async def exists(self) -> bool:
        return self._blob_name in self._container_dict


class FakeContainerClient:
    def __init__(self, storage_dict: dict[str, dict[str, bytes]], container_name: str) -> None:
        self._container_name = container_name
        if container_name not in storage_dict:
            storage_dict[container_name] = {}
        self._blobs = storage_dict[container_name]

    def get_blob_client(self, blob_name: str) -> FakeBlobClient:
        return FakeBlobClient(self._blobs, blob_name)


class FakeBlobServiceClient:
    def __init__(self) -> None:
        self.storage: dict[str, dict[str, bytes]] = {}

    def get_container_client(self, container_name: str) -> FakeContainerClient:
        return FakeContainerClient(self.storage, container_name)


class FakeCosmosBatchClient:
    """Offline double for Cosmos transactional batch."""

    def __init__(self) -> None:
        self.documents: dict[tuple[str, str], dict] = {}
        self._etag_counter = 0

    async def query_items(self, *, query, parameters, partition_key):
        values = {param["name"]: param["value"] for param in parameters}
        return [
            dict(doc)
            for (pk, _), doc in self.documents.items()
            if pk == partition_key
            and doc.get("record_type") == values.get("@record_type")
            and doc.get("household_id") == values.get("@household_id")
            and doc.get("image_ref") == values.get("@image_ref")
        ]

    async def read_item(self, *, item_id, partition_key):
        doc = self.documents.get((partition_key, item_id))
        return None if doc is None else dict(doc)

    async def execute_batch(self, *, partition_key, operations):
        staged = {}
        next_etag = self._etag_counter
        for index, op in enumerate(operations):
            doc = dict(op.document)
            if doc.get("household_id") != partition_key:
                raise ValueError("Batch document does not match household partition.")
            key = (partition_key, doc["id"])
            if isinstance(op, CreateDocument):
                if key in self.documents or key in staged:
                    raise BatchOperationConflict(index)
            elif isinstance(op, ReplaceDocument):
                current = self.documents.get(key)
                if current is None or current.get("_etag") != op.if_match_etag:
                    raise BatchOperationConflict(index)
            next_etag += 1
            doc["_etag"] = f'"etag-{next_etag}"'
            staged[key] = doc

        self.documents.update(staged)
        self._etag_counter = next_etag


@pytest.fixture
def fake_cosmos():
    return FakeCosmosBatchClient()


@pytest.fixture
def mapping_port(fake_cosmos):
    read_repo = CosmosReadRepository(fake_cosmos)
    return CosmosMediaMappingStorage(read_repository=read_repo, write_client=fake_cosmos)


@pytest.fixture
def fake_blob_service():
    return FakeBlobServiceClient()


@pytest.fixture
def blob_storage(fake_blob_service, mapping_port):
    return AzureBlobMediaStorage(
        blob_service_client=fake_blob_service,
        container_name="media",
        mapping_port=mapping_port,
        auto_process=False,
    )


@pytest.mark.anyio
async def test_create_upload_authorization_registers_allocated(blob_storage, mapping_port):
    req = UploadAuthorizationRequest(
        household_id="home-dev",
        filename="milk.jpg",
        content_type=AllowedMediaContentType.IMAGE_JPEG,
        file_size_bytes=1024,
    )
    auth = await blob_storage.create_upload_authorization(req)
    assert auth.upload_url.startswith("http://127.0.0.1:8000/staging/upload/staging-token-")
    assert auth.image_ref.startswith("blob://households/home-dev/media/")
    assert auth.image_ref.endswith(".jpg")

    snapshot = await mapping_port.find_by_image_ref(
        household_id="home-dev", image_ref=auth.image_ref
    )
    assert snapshot is not None
    assert snapshot.record.status is MediaProcessingStatus.ALLOCATED
    assert snapshot.record.ttl_seconds == 86400


@pytest.mark.anyio
async def test_receive_staged_upload_writes_blob_and_transitions_received(
    blob_storage, mapping_port, fake_blob_service
):
    req = UploadAuthorizationRequest(
        household_id="home-dev",
        filename="apple.png",
        content_type=AllowedMediaContentType.IMAGE_PNG,
        file_size_bytes=4,
    )
    auth = await blob_storage.create_upload_authorization(req)
    token = auth.upload_url.split("/")[-1]

    payload = b"test-apple-bytes"
    hh, ref = await blob_storage.receive_staged_upload(token, payload)
    assert hh == "home-dev"
    assert ref == auth.image_ref

    # 驗證 Azure Blob Container 中有寫入真實位元組
    blob_path = ref.removeprefix("blob://").lstrip("/")
    stored_bytes = fake_blob_service.storage["media"].get(blob_path)
    assert stored_bytes == payload

    # 驗證 Cosmos 狀態轉為 RECEIVED
    snapshot = await mapping_port.find_by_image_ref(
        household_id="home-dev", image_ref=auth.image_ref
    )
    assert snapshot is not None
    assert snapshot.record.status is MediaProcessingStatus.RECEIVED
    assert snapshot.record.file_size_bytes == len(payload)
    assert snapshot.record.sha256_hash == hashlib.sha256(payload).hexdigest()


@pytest.mark.anyio
async def test_get_media_bytes_and_household_boundary(blob_storage):
    req = UploadAuthorizationRequest(
        household_id="home-dev",
        filename="banana.jpg",
        content_type=AllowedMediaContentType.IMAGE_JPEG,
        file_size_bytes=5,
    )
    auth = await blob_storage.create_upload_authorization(req)
    token = auth.upload_url.split("/")[-1]
    await blob_storage.receive_staged_upload(token, b"banana-data")

    # 本家庭正常讀取
    data = await blob_storage.get_media_bytes("home-dev", auth.image_ref)
    assert data == b"banana-data"

    # 跨家庭讀取一律拒絕
    data_cross = await blob_storage.get_media_bytes("home-other", auth.image_ref)
    assert data_cross is None


@pytest.mark.anyio
async def test_complete_processing_uploads_artifact_json(
    blob_storage, mapping_port, fake_blob_service
):
    req = UploadAuthorizationRequest(
        household_id="home-dev",
        filename="cereal.jpg",
        content_type=AllowedMediaContentType.IMAGE_JPEG,
        file_size_bytes=6,
    )
    auth = await blob_storage.create_upload_authorization(req)
    token = auth.upload_url.split("/")[-1]
    await blob_storage.receive_staged_upload(token, b"cereal")
    # 未完成前，Agent 門禁阻斷
    assert await blob_storage.verify_media_exists("home-dev", auth.image_ref) is False

    await blob_storage.begin_processing("home-dev", auth.image_ref)

    artifact_json = '{"items": [{"raw_name": "cereal", "quantity": 1}]}'
    artifact_ref = await blob_storage.complete_processing("home-dev", auth.image_ref, artifact_json)
    assert "derived/vision_v1.json" in artifact_ref

    # 完成後，Agent 門禁放行
    assert await blob_storage.verify_media_exists("home-dev", auth.image_ref) is True

    # 驗證衍生工件寫入 Blob
    artifact_blob_path = artifact_ref.removeprefix("blob://").lstrip("/")
    expected_payload = artifact_json.encode("utf-8")
    assert fake_blob_service.storage["media"].get(artifact_blob_path) == expected_payload

    # 驗證 Cosmos 狀態為 COMPLETED
    snapshot = await mapping_port.find_by_image_ref(
        household_id="home-dev", image_ref=auth.image_ref
    )
    assert snapshot is not None
    assert snapshot.record.status is MediaProcessingStatus.COMPLETED
    assert snapshot.record.artifact_ref == artifact_ref


@pytest.mark.anyio
async def test_upload_timeout_marks_failed(blob_storage, mapping_port):
    req = UploadAuthorizationRequest(
        household_id="home-dev",
        filename="late.jpg",
        content_type=AllowedMediaContentType.IMAGE_JPEG,
        file_size_bytes=2,
    )
    auth = await blob_storage.create_upload_authorization(req)
    token = auth.upload_url.split("/")[-1]

    # 手動令 token 過期
    blob_storage._pending_tokens[token]["expires_at"] = time.time() - 10

    with pytest.raises(TimeoutError):
        await blob_storage.receive_staged_upload(token, b"late")

    snapshot = await mapping_port.find_by_image_ref(
        household_id="home-dev", image_ref=auth.image_ref
    )
    assert snapshot is not None
    assert snapshot.record.status is MediaProcessingStatus.FAILED
    assert snapshot.record.failure_reason is MediaFailureReason.UPLOAD_TIMEOUT


@pytest.mark.anyio
async def test_upload_validation_failure_marks_failed(blob_storage, mapping_port):
    req = UploadAuthorizationRequest(
        household_id="home-dev",
        filename="empty.jpg",
        content_type=AllowedMediaContentType.IMAGE_JPEG,
        file_size_bytes=100,
    )
    auth = await blob_storage.create_upload_authorization(req)
    token = auth.upload_url.split("/")[-1]

    # 空檔案
    with pytest.raises(ValueError, match="Payload cannot be empty"):
        await blob_storage.receive_staged_upload(token, b"")

    snapshot = await mapping_port.find_by_image_ref(
        household_id="home-dev", image_ref=auth.image_ref
    )
    assert snapshot is not None
    assert snapshot.record.status is MediaProcessingStatus.FAILED
    assert snapshot.record.failure_reason is MediaFailureReason.VALIDATION_FAILED


def test_dependency_dual_mode_selection():
    # 1. 無任何 Azure 變數 -> 退回 LocalStagingMediaStorage
    storage_local = _create_media_storage_service(environment={})
    assert isinstance(storage_local, LocalStagingMediaStorage)

    # 2. 有連線字串時 (非同步解析) -> 嘗試實體化 AzureBlobMediaStorage
    # 使用標準 dummy connection string 測試裝配語法
    dummy_conn = (
        "DefaultEndpointsProtocol=https;"
        "AccountName=testaccount;"
        "AccountKey=dGVzdGtleQ==;"
        "EndpointSuffix=core.windows.net"
    )
    storage_blob = _create_media_storage_service(
        environment={"AZURE_STORAGE_CONNECTION_STRING": dummy_conn}
    )
    assert isinstance(storage_blob, AzureBlobMediaStorage)


@pytest.mark.anyio
async def test_receive_staged_upload_auto_process_ingest(fake_blob_service, mapping_port):
    """驗證 auto_process=True 時，上傳二進位檔案自動推進至 COMPLETED 並上傳衍生工件。"""
    storage = AzureBlobMediaStorage(
        blob_service_client=fake_blob_service,
        container_name="media",
        mapping_port=mapping_port,
        auto_process=True,
    )
    req = UploadAuthorizationRequest(
        household_id="home-dev",
        filename="auto.jpg",
        content_type=AllowedMediaContentType.IMAGE_JPEG,
        file_size_bytes=4,
    )
    auth = await storage.create_upload_authorization(req)
    token = auth.upload_url.split("/")[-1]

    # 上傳檔案
    await storage.receive_staged_upload(token, b"auto")

    # 驗證狀態已自動推進至 COMPLETED
    snapshot = await mapping_port.find_by_image_ref(
        household_id="home-dev", image_ref=auth.image_ref
    )
    assert snapshot is not None
    assert snapshot.record.status is MediaProcessingStatus.COMPLETED
    assert snapshot.record.artifact_ref is not None

    # Agent 門禁放行
    assert await storage.verify_media_exists("home-dev", auth.image_ref) is True


@pytest.mark.anyio
async def test_fail_processing_error_propagation_on_timeout(fake_blob_service, mapping_port):
    """驗證上傳逾時若 FAILED 狀態寫入失敗，例外會明確向上拋出，不再被靜默吞噬。"""
    storage = AzureBlobMediaStorage(
        blob_service_client=fake_blob_service,
        container_name="media",
        mapping_port=mapping_port,
    )
    req = UploadAuthorizationRequest(
        household_id="home-dev",
        filename="fail.jpg",
        content_type=AllowedMediaContentType.IMAGE_JPEG,
        file_size_bytes=10,
    )
    auth = await storage.create_upload_authorization(req)
    token = auth.upload_url.split("/")[-1]

    storage._pending_tokens[token]["expires_at"] = time.time() - 1

    async def mock_fail_error(*args, **kwargs):
        raise RuntimeError("Cosmos DB write failure during fail_processing")

    storage.fail_processing = mock_fail_error

    with pytest.raises(RuntimeError, match="Cosmos DB write failure"):
        await storage.receive_staged_upload(token, b"fail_data")


@pytest.mark.anyio
async def test_verify_media_exists_propagates_database_error(fake_blob_service, mapping_port):
    """驗證 Cosmos DB 查詢異常時，verify_media_exists 嚴格向外傳播，不偽裝為 False。"""
    storage = AzureBlobMediaStorage(
        blob_service_client=fake_blob_service,
        container_name="media",
        mapping_port=mapping_port,
    )

    async def mock_query_error(*args, **kwargs):
        raise ConnectionResetError("Cosmos DB connection reset")

    mapping_port.find_completed_by_image_ref = mock_query_error

    with pytest.raises(ConnectionResetError, match="Cosmos DB connection reset"):
        await storage.verify_media_exists("home-dev", "blob://households/home-dev/media/test.jpg")


@pytest.mark.anyio
async def test_begin_processing_idempotent_on_completed(fake_blob_service, mapping_port):
    """驗證對 COMPLETED 媒體重複調用 begin_processing 與 complete_processing 安全等冪。"""
    storage = AzureBlobMediaStorage(
        blob_service_client=fake_blob_service,
        container_name="media",
        mapping_port=mapping_port,
        auto_process=True,
    )
    req = UploadAuthorizationRequest(
        household_id="home-dev",
        filename="repeat.jpg",
        content_type=AllowedMediaContentType.IMAGE_JPEG,
        file_size_bytes=6,
    )
    auth = await storage.create_upload_authorization(req)
    token = auth.upload_url.split("/")[-1]

    await storage.receive_staged_upload(token, b"repeat")

    # 二次調用 begin_processing：安全返回，不引發 MediaMappingConflict
    await storage.begin_processing("home-dev", auth.image_ref)

    # 二次調用 complete_processing：安全回傳 artifact_ref
    ref = await storage.complete_processing("home-dev", auth.image_ref)
    assert "derived/vision_v1.json" in ref

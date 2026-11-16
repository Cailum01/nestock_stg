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
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from azure.storage.blob import ContentSettings

from app.core.dependencies import _create_media_storage_service
from app.models.domain import (
    MediaFailureReason,
    MediaProcessingStatus,
)
from app.models.storage import (
    AllowedMediaContentType,
    UploadAuthorizationRequest,
)
from app.models.vision import VisionAnalysisResult
from app.services.cosmos_read import CosmosReadRepository
from app.services.cosmos_write import (
    BatchOperationConflict,
    CreateDocument,
    ReplaceDocument,
)
from app.services.media_mapping import (
    CosmosMediaMappingStorage,
    build_media_mapping_retention_update,
)
from app.services.provisional_storage.azure_blob_adapter import AzureBlobMediaStorage
from app.services.provisional_storage.local_staging_adapter import LocalStagingMediaStorage
from app.services.vision import MockVisionService


class FakeBlobProperties:
    def __init__(
        self,
        name: str,
        metadata: dict[str, str] | None = None,
        tags: dict[str, str] | None = None,
        creation_time: datetime | None = None,
        content_settings: Any | None = None,
    ) -> None:
        self.name = name
        self.metadata = dict(metadata or {})
        self.tags = dict(tags or {})
        self.creation_time = creation_time or datetime.now(UTC)
        self.content_settings = content_settings or ContentSettings()


class FakeBlobClient:
    def __init__(
        self,
        container_dict: dict[str, bytes],
        metadata_dict: dict[str, dict[str, str]],
        tags_dict: dict[str, dict[str, str]],
        creation_time_dict: dict[str, datetime],
        content_settings_dict: dict[str, Any],
        blob_name: str,
    ) -> None:
        self._container_dict = container_dict
        self._metadata_dict = metadata_dict
        self._tags_dict = tags_dict
        self._creation_time_dict = creation_time_dict
        self._content_settings_dict = content_settings_dict
        self._blob_name = blob_name

    async def upload_blob(self, data: bytes | str, overwrite: bool = True, **kwargs) -> None:
        payload = data.encode("utf-8") if isinstance(data, str) else data
        self._container_dict[self._blob_name] = payload
        meta = kwargs.get("metadata")
        if isinstance(meta, dict):
            self._metadata_dict[self._blob_name] = {str(k): str(v) for k, v in meta.items()}
        elif self._blob_name not in self._metadata_dict:
            self._metadata_dict[self._blob_name] = {}
        tags = kwargs.get("tags")
        if isinstance(tags, dict):
            self._tags_dict[self._blob_name] = {str(k): str(v) for k, v in tags.items()}
        elif self._blob_name not in self._tags_dict:
            self._tags_dict[self._blob_name] = {}
        self._creation_time_dict[self._blob_name] = datetime.now(UTC)
        cs = kwargs.get("content_settings")
        if cs is not None:
            self._content_settings_dict[self._blob_name] = cs
        elif "content_type" in kwargs:
            self._content_settings_dict[self._blob_name] = ContentSettings(
                content_type=kwargs["content_type"]
            )
        else:
            self._content_settings_dict[self._blob_name] = ContentSettings(
                content_type="application/octet-stream"
            )

    async def set_blob_metadata(self, metadata: dict[str, str] | None = None, **kwargs) -> None:
        if self._blob_name not in self._container_dict:
            raise KeyError(f"Blob {self._blob_name} does not exist.")
        self._metadata_dict[self._blob_name] = {str(k): str(v) for k, v in (metadata or {}).items()}

    async def set_blob_tags(self, tags: dict[str, str] | None = None, **kwargs) -> None:
        if self._blob_name not in self._container_dict:
            raise KeyError(f"Blob {self._blob_name} does not exist.")
        self._tags_dict[self._blob_name] = {str(k): str(v) for k, v in (tags or {}).items()}

    async def get_blob_tags(self, **kwargs) -> dict[str, str]:
        if self._blob_name not in self._container_dict:
            raise KeyError(f"Blob {self._blob_name} does not exist.")
        return dict(self._tags_dict.get(self._blob_name, {}))

    async def get_blob_properties(self, **kwargs) -> FakeBlobProperties:
        if self._blob_name not in self._container_dict:
            raise KeyError(f"Blob {self._blob_name} does not exist.")
        return FakeBlobProperties(
            name=self._blob_name,
            metadata=self._metadata_dict.get(self._blob_name, {}),
            tags=self._tags_dict.get(self._blob_name, {}),
            creation_time=self._creation_time_dict.get(self._blob_name),
            content_settings=self._content_settings_dict.get(self._blob_name),
        )

    async def delete_blob(self, **kwargs) -> None:
        self._container_dict.pop(self._blob_name, None)
        self._metadata_dict.pop(self._blob_name, None)
        self._tags_dict.pop(self._blob_name, None)
        self._creation_time_dict.pop(self._blob_name, None)
        self._content_settings_dict.pop(self._blob_name, None)

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
    def __init__(
        self,
        storage_dict: dict[str, dict[str, bytes]],
        metadata_store: dict[str, dict[str, dict[str, str]]],
        tags_store: dict[str, dict[str, dict[str, str]]],
        creation_time_store: dict[str, dict[str, datetime]],
        content_settings_store: dict[str, dict[str, Any]],
        container_name: str,
    ) -> None:
        self._container_name = container_name
        if container_name not in storage_dict:
            storage_dict[container_name] = {}
        if container_name not in metadata_store:
            metadata_store[container_name] = {}
        if container_name not in tags_store:
            tags_store[container_name] = {}
        if container_name not in creation_time_store:
            creation_time_store[container_name] = {}
        if container_name not in content_settings_store:
            content_settings_store[container_name] = {}
        self._blobs = storage_dict[container_name]
        self._metadata = metadata_store[container_name]
        self._tags = tags_store[container_name]
        self._creation_times = creation_time_store[container_name]
        self._content_settings = content_settings_store[container_name]

    def get_blob_client(self, blob_name: str) -> FakeBlobClient:
        return FakeBlobClient(
            self._blobs,
            self._metadata,
            self._tags,
            self._creation_times,
            self._content_settings,
            blob_name,
        )

    def list_blobs(self, name_starts_with: str | None = None, **kwargs):
        prefix = name_starts_with or ""
        items = [
            FakeBlobProperties(
                name=name,
                metadata=self._metadata.get(name, {}),
                tags=self._tags.get(name, {}),
                creation_time=self._creation_times.get(name),
                content_settings=self._content_settings.get(name),
            )
            for name in list(self._blobs.keys())
            if name.startswith(prefix)
        ]

        class AsyncBlobPager:
            def __init__(self, props_list):
                self._props = props_list

            def __aiter__(self):
                self._iter = iter(self._props)
                return self

            async def __anext__(self):
                try:
                    return next(self._iter)
                except StopIteration as exc:
                    raise StopAsyncIteration from exc

            def __iter__(self):
                return iter(self._props)

        return AsyncBlobPager(items)


class FakeBlobServiceClient:
    def __init__(self) -> None:
        self.storage: dict[str, dict[str, bytes]] = {}
        self.metadata: dict[str, dict[str, dict[str, str]]] = {}
        self.tags: dict[str, dict[str, dict[str, str]]] = {}
        self.creation_times: dict[str, dict[str, datetime]] = {}
        self.content_settings: dict[str, dict[str, Any]] = {}

    def get_container_client(self, container_name: str) -> FakeContainerClient:
        return FakeContainerClient(
            self.storage,
            self.metadata,
            self.tags,
            self.creation_times,
            self.content_settings,
            container_name,
        )


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
    """驗證 auto_process=True 時，上傳檔案必須執行分析並寫入衍生工件後才推進至 COMPLETED。"""
    storage = AzureBlobMediaStorage(
        blob_service_client=fake_blob_service,
        container_name="media",
        mapping_port=mapping_port,
        auto_process=True,
        vision_service=MockVisionService(),
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

    # 驗證衍生工件實際已寫入 Blob 容器且為合法 VisionAnalysisResult JSON
    artifact_blob_path = snapshot.record.artifact_ref.removeprefix("blob://").lstrip("/")
    raw_artifact = fake_blob_service.storage["media"].get(artifact_blob_path)
    assert raw_artifact is not None and len(raw_artifact) > 0
    derived_bytes = await storage.get_derived_artifact("home-dev", auth.image_ref)
    assert derived_bytes == raw_artifact
    parsed_result = VisionAnalysisResult.model_validate_json(derived_bytes)
    assert parsed_result.image_ref == auth.image_ref

    # Agent 門禁放行
    assert await storage.verify_media_exists("home-dev", auth.image_ref) is True


@pytest.mark.anyio
async def test_complete_processing_rejects_missing_artifact(blob_storage, mapping_port):
    """驗證未處於 COMPLETED 時呼叫 complete_processing 若缺工件，拋出 ValueError。"""
    req = UploadAuthorizationRequest(
        household_id="home-dev",
        filename="no-artifact.jpg",
        content_type=AllowedMediaContentType.IMAGE_JPEG,
        file_size_bytes=6,
    )
    auth = await blob_storage.create_upload_authorization(req)
    token = auth.upload_url.split("/")[-1]
    await blob_storage.receive_staged_upload(token, b"rawimg")
    await blob_storage.begin_processing("home-dev", auth.image_ref)

    with pytest.raises(ValueError):
        await blob_storage.complete_processing("home-dev", auth.image_ref, None)

    with pytest.raises(ValueError):
        await blob_storage.complete_processing("home-dev", auth.image_ref, "")

    snapshot = await mapping_port.find_by_image_ref(
        household_id="home-dev", image_ref=auth.image_ref
    )
    assert snapshot is not None
    assert snapshot.record.status is MediaProcessingStatus.PROCESSING
    assert await blob_storage.verify_media_exists("home-dev", auth.image_ref) is False


@pytest.mark.anyio
async def test_azure_blob_verify_media_exists_fails_closed_without_mapping_port(fake_blob_service):
    """驗證當 mapping_port 為 None 時，即使 Blob 存在，verify_media_exists 仍嚴格回傳 False。"""
    storage = AzureBlobMediaStorage(
        blob_service_client=fake_blob_service,
        container_name="media",
        mapping_port=None,
        auto_process=False,
    )
    req = UploadAuthorizationRequest(
        household_id="home-dev",
        filename="unmapped.jpg",
        content_type=AllowedMediaContentType.IMAGE_JPEG,
        file_size_bytes=8,
    )
    auth = await storage.create_upload_authorization(req)
    token = auth.upload_url.split("/")[-1]
    await storage.receive_staged_upload(token, b"unmapped")

    blob_path = auth.image_ref.removeprefix("blob://").lstrip("/")
    assert fake_blob_service.storage["media"].get(blob_path) == b"unmapped"
    assert await storage.verify_media_exists("home-dev", auth.image_ref) is False


@pytest.mark.anyio
async def test_azure_blob_orphan_expiration_deletes_blobs_and_preserves_attached_media(
    fake_blob_service, mapping_port, fake_cosmos
):
    """驗證 24h 孤立期限過後，cleanup_expired_blobs 刪除孤立檔案，並保留已附加之媒體。"""
    storage = AzureBlobMediaStorage(
        blob_service_client=fake_blob_service,
        container_name="media",
        mapping_port=mapping_port,
        auto_process=True,
        vision_service=MockVisionService(),
    )
    req_orphan = UploadAuthorizationRequest(
        household_id="home-dev",
        filename="orphan.jpg",
        content_type=AllowedMediaContentType.IMAGE_JPEG,
        file_size_bytes=6,
    )
    auth_orphan = await storage.create_upload_authorization(req_orphan)
    token_orphan = auth_orphan.upload_url.split("/")[-1]
    await storage.receive_staged_upload(token_orphan, b"orphan")

    req_attached = UploadAuthorizationRequest(
        household_id="home-dev",
        filename="attached.jpg",
        content_type=AllowedMediaContentType.IMAGE_JPEG,
        file_size_bytes=8,
    )
    auth_attached = await storage.create_upload_authorization(req_attached)
    token_attached = auth_attached.upload_url.split("/")[-1]
    await storage.receive_staged_upload(token_attached, b"attached")

    snap_orphan = await mapping_port.find_by_image_ref(
        household_id="home-dev", image_ref=auth_orphan.image_ref
    )
    snap_attached = await mapping_port.find_by_image_ref(
        household_id="home-dev", image_ref=auth_attached.image_ref
    )
    assert snap_orphan is not None and snap_orphan.record.artifact_ref is not None
    assert snap_attached is not None and snap_attached.record.artifact_ref is not None

    # 透過 B1 retention 合約將 auth_attached 標記為業務永久保留 (ttl_seconds=None)
    retention_op = build_media_mapping_retention_update(snap_attached, ttl_seconds=None)
    await fake_cosmos.execute_batch(partition_key="home-dev", operations=(retention_op,))

    future_now = datetime.now(UTC) + timedelta(seconds=86410)
    purged = await storage.cleanup_expired_blobs(now=future_now)

    assert auth_orphan.image_ref in purged
    assert auth_attached.image_ref not in purged

    orphan_raw_path = auth_orphan.image_ref.removeprefix("blob://").lstrip("/")
    orphan_art_path = snap_orphan.record.artifact_ref.removeprefix("blob://").lstrip("/")
    assert orphan_raw_path not in fake_blob_service.storage["media"]
    assert orphan_art_path not in fake_blob_service.storage["media"]
    assert await storage.get_media_bytes("home-dev", auth_orphan.image_ref) is None

    attached_raw_path = auth_attached.image_ref.removeprefix("blob://").lstrip("/")
    attached_art_path = snap_attached.record.artifact_ref.removeprefix("blob://").lstrip("/")
    assert fake_blob_service.storage["media"].get(attached_raw_path) == b"attached"
    assert attached_art_path in fake_blob_service.storage["media"]
    assert await storage.get_media_bytes("home-dev", auth_attached.image_ref) == b"attached"


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
        vision_service=MockVisionService(),
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


@pytest.mark.anyio
async def test_blob_delete_failure_prevents_purged_flag_and_allows_retry(
    fake_blob_service, mapping_port
):
    """驗證當 Blob 刪除拋出 OSError 時，記錄不可標記為 purged，且允許後續重試。"""
    storage = AzureBlobMediaStorage(
        blob_service_client=fake_blob_service,
        container_name="media",
        mapping_port=mapping_port,
        auto_process=False,
    )
    req = UploadAuthorizationRequest(
        household_id="home-dev",
        filename="fail_del.jpg",
        content_type=AllowedMediaContentType.IMAGE_JPEG,
        file_size_bytes=6,
    )
    auth = await storage.create_upload_authorization(req)
    token = auth.upload_url.split("/")[-1]
    await storage.receive_staged_upload(token, b"fail_del_bytes")

    blob_path = auth.image_ref.removeprefix("blob://").lstrip("/")

    # 模擬磁碟或網路異常
    original_delete = FakeBlobClient.delete_blob

    async def mock_broken_delete(self, *args, **kwargs):
        raise OSError("Simulated IO disk error")

    FakeBlobClient.delete_blob = mock_broken_delete

    future_now = datetime.now(UTC) + timedelta(seconds=86410)
    try:
        purged = await storage.cleanup_expired_blobs(now=future_now)

        # 刪除失敗時，不可包含在回傳之 purged 清單
        assert auth.image_ref not in purged
        # 內部標記不可為 purged=True
        rec = storage._retention_records.get(("home-dev", auth.image_ref))
        assert rec is not None
        assert rec["purged"] is False
        # Blob 仍存在於存儲中
        assert blob_path in fake_blob_service.storage["media"]
    finally:
        FakeBlobClient.delete_blob = original_delete

    # 恢復正常後，重試刪除應成功
    purged_retry = await storage.cleanup_expired_blobs(now=future_now)
    assert auth.image_ref in purged_retry
    assert rec["purged"] is True
    assert blob_path not in fake_blob_service.storage["media"]


@pytest.mark.anyio
async def test_blob_download_and_exists_reraises_non_404_errors(fake_blob_service):
    """驗證 Blob 下載與存在檢查：僅 404 回傳 None/False，非 404 異常向上拋出。"""
    storage = AzureBlobMediaStorage(
        blob_service_client=fake_blob_service,
        container_name="media",
    )

    class ResourceNotFoundError(Exception):
        status_code = 404

    original_download = FakeBlobClient.download_blob
    original_exists = FakeBlobClient.exists

    # 404 不存在：安全回傳 None 與 False
    async def mock_404(*args, **kwargs):
        raise ResourceNotFoundError("Blob not found")

    FakeBlobClient.download_blob = mock_404
    FakeBlobClient.exists = mock_404
    try:
        assert await storage._download_from_blob("missing.jpg") is None
        assert await storage._blob_exists("missing.jpg") is False
    finally:
        FakeBlobClient.download_blob = original_download
        FakeBlobClient.exists = original_exists

    # 非 404 系統/網路異常：強制向上傳播
    async def mock_500(*args, **kwargs):
        raise RuntimeError("Azure Blob 503 Service Unavailable")

    FakeBlobClient.download_blob = mock_500
    FakeBlobClient.exists = mock_500
    try:
        with pytest.raises(RuntimeError, match="503 Service Unavailable"):
            await storage._download_from_blob("network_error.jpg")
        with pytest.raises(RuntimeError, match="503 Service Unavailable"):
            await storage._blob_exists("network_error.jpg")
    finally:
        FakeBlobClient.download_blob = original_download
        FakeBlobClient.exists = original_exists


@pytest.mark.anyio
async def test_retain_media_reraises_and_preserves_state_on_metadata_failure(
    fake_blob_service, mapping_port
):
    """驗證當 set_blob_metadata 失敗時，retain_media 向上拋出例外，且內部狀態不變更為 attached。"""
    storage = AzureBlobMediaStorage(
        blob_service_client=fake_blob_service,
        container_name="media",
        mapping_port=mapping_port,
        auto_process=False,
    )
    req = UploadAuthorizationRequest(
        household_id="home-dev",
        filename="meta_fail.jpg",
        content_type=AllowedMediaContentType.IMAGE_JPEG,
        file_size_bytes=6,
    )
    auth = await storage.create_upload_authorization(req)
    token = auth.upload_url.split("/")[-1]
    await storage.receive_staged_upload(token, b"meta_bytes")

    original_meta = FakeBlobClient.set_blob_metadata

    async def mock_broken_meta(self, *args, **kwargs):
        raise OSError("Failed to communicate with Azure Blob metadata endpoint")

    FakeBlobClient.set_blob_metadata = mock_broken_meta
    try:
        with pytest.raises(OSError, match="Failed to communicate"):
            await storage.retain_media("home-dev", auth.image_ref)

        rec = storage._retention_records.get(("home-dev", auth.image_ref))
        assert rec is not None
        assert rec["retention"] != "attached"
        assert rec.get("expires_at") is not None
    finally:
        FakeBlobClient.set_blob_metadata = original_meta


def test_create_media_storage_service_fails_fast_on_bad_cloud_config():
    """驗證當環境配置了 Blob 連線資訊但初始化失敗時，嚴格拋出例外，絕不靜默降級至本機記憶體。"""
    from unittest.mock import patch

    bad_env = {"AZURE_STORAGE_CONNECTION_STRING": "InvalidConnectionString"}
    with patch(
        "azure.storage.blob.BlobServiceClient.from_connection_string",
        side_effect=ValueError("Invalid connection string format"),
    ):
        with pytest.raises(RuntimeError, match="Azure Blob Storage configuration failed"):
            _create_media_storage_service(environment=bad_env)

    bad_endpoint_env = {"AZURE_STORAGE_BLOB_ENDPOINT": "https://invalid.blob.core.windows.net/"}
    with patch(
        "azure.storage.blob.BlobServiceClient.__init__",
        side_effect=ValueError("Invalid endpoint URL"),
    ):
        with pytest.raises(RuntimeError, match="Azure Blob Storage configuration failed"):
            _create_media_storage_service(environment=bad_endpoint_env)


@pytest.mark.anyio
async def test_unretain_media_restores_orphan_metadata_and_expiration(
    blob_storage, fake_blob_service
):
    """驗證 unretain_media 可將已保留媒體還原為孤立狀態並重新配置過期時間。"""
    req = UploadAuthorizationRequest(
        household_id="home-dev",
        filename="milk.jpg",
        content_type=AllowedMediaContentType.IMAGE_JPEG,
        file_size_bytes=6,
    )
    auth = await blob_storage.create_upload_authorization(req)
    token = auth.upload_url.split("/")[-1]
    await blob_storage.receive_staged_upload(token, b"milk12")

    # 1. 標記為保留 (attached)
    assert await blob_storage.retain_media("home-dev", auth.image_ref) is True
    rec = blob_storage._retention_records[("home-dev", auth.image_ref)]
    assert rec["retention"] == "attached"
    assert rec["expires_at"] is None

    # 2. 解除保留 (unretain)，設定 TTL 為 3600 秒
    now_dt = datetime.now(UTC)
    assert await blob_storage.unretain_media("home-dev", auth.image_ref, ttl_seconds=3600) is True
    rec_after = blob_storage._retention_records[("home-dev", auth.image_ref)]
    assert rec_after["retention"] == "orphan"
    assert rec_after["expires_at"] is not None
    assert rec_after["expires_at"] > now_dt

    # 驗證 Blob 元資料與 Tags 已同步為 orphan
    blob_path = auth.image_ref.removeprefix("blob://").lstrip("/")
    client = fake_blob_service.get_container_client("media").get_blob_client(blob_path)
    props = await client.get_blob_properties()
    assert props.metadata["retention"] == "orphan"
    assert props.metadata["expires_at"] != ""
    assert props.tags["retention"] == "orphan"
    assert props.tags["household_id"] == "home-dev"
    assert props.creation_time >= now_dt

    # 3. 未到期時 cleanup 不應清除
    purged_early = await blob_storage.cleanup_expired_blobs(now=now_dt + timedelta(seconds=1800))
    assert auth.image_ref not in purged_early

    # 4. 逾期後 cleanup 應安全清除
    purged_late = await blob_storage.cleanup_expired_blobs(now=now_dt + timedelta(seconds=4000))
    assert auth.image_ref in purged_late
    assert rec_after["purged"] is True

    # 5. 已清除之媒體再次呼叫 unretain 回傳 False
    assert await blob_storage.unretain_media("home-dev", auth.image_ref) is False
    assert (
        await blob_storage.unretain_media(
            "home-dev", "blob://households/home-dev/media/unknown.jpg"
        )
        is False
    )


@pytest.mark.anyio
async def test_retain_media_reraises_on_tags_failure(fake_blob_service, mapping_port):
    """驗證當 set_blob_tags 失敗時，retain_media 向上拋出例外。"""
    storage = AzureBlobMediaStorage(
        blob_service_client=fake_blob_service,
        container_name="media",
        mapping_port=mapping_port,
        auto_process=False,
    )
    req = UploadAuthorizationRequest(
        household_id="home-dev",
        filename="tag_fail.jpg",
        content_type=AllowedMediaContentType.IMAGE_JPEG,
        file_size_bytes=6,
    )
    auth = await storage.create_upload_authorization(req)
    token = auth.upload_url.split("/")[-1]
    await storage.receive_staged_upload(token, b"tag_bytes")

    original_tags = FakeBlobClient.set_blob_tags

    async def mock_broken_tags(self, *args, **kwargs):
        raise OSError("Failed to communicate with Azure Blob tags endpoint")

    FakeBlobClient.set_blob_tags = mock_broken_tags
    try:
        with pytest.raises(OSError, match="Failed to communicate with Azure Blob tags endpoint"):
            await storage.retain_media("home-dev", auth.image_ref)
    finally:
        FakeBlobClient.set_blob_tags = original_tags


@pytest.mark.anyio
async def test_azure_lifecycle_policy_preserves_attached_and_respects_unretain_grace_period(
    blob_storage, fake_blob_service
):
    """驗證 Azure Lifecycle 政策（blobIndexMatch: retention == orphan）：

    1. 已保留媒體（retention == attached）永遠不會被 Lifecycle 政策選中刪除。
    2. 新上傳孤立媒體在 24 小時後符合刪除條件。
    3. 舊媒體解除保留（unretain）後物理刷新 CreationTime，保證 24 小時寬限期。
    """
    container = fake_blob_service.get_container_client("media")

    def simulate_azure_lifecycle_policy_purge(current_time: datetime) -> list[str]:
        """模擬 Azure 原生 Lifecycle Management 執行引擎：

        Filters: prefixMatch: 'households/', blobIndexMatch: retention == 'orphan'
        Action: delete if daysAfterCreationGreaterThan: 1 (creation_time + 86400s < current_time)
        """
        deleted = []
        for blob_name in list(container._blobs.keys()):
            if not blob_name.startswith("households/"):
                continue
            tags = container._tags.get(blob_name, {})
            # 關鍵門禁：只有 tag retention == orphan 的 Blob 才會被政策鎖定
            if tags.get("retention") != "orphan":
                continue
            c_time = container._creation_times.get(blob_name)
            if c_time is None:
                continue
            # daysAfterCreationGreaterThan: 1 相當於建立時間超過 86400 秒
            if (current_time - c_time).total_seconds() > 86400:
                container._blobs.pop(blob_name, None)
                container._metadata.pop(blob_name, None)
                container._tags.pop(blob_name, None)
                container._creation_times.pop(blob_name, None)
                deleted.append(blob_name)
        return deleted

    # 1. 建立「持久保留媒體 A」(attached)
    req_a = UploadAuthorizationRequest(
        household_id="home-dev",
        filename="attached_item.jpg",
        content_type=AllowedMediaContentType.IMAGE_JPEG,
        file_size_bytes=10,
    )
    auth_a = await blob_storage.create_upload_authorization(req_a)
    token_a = auth_a.upload_url.split("/")[-1]
    await blob_storage.receive_staged_upload(token_a, b"attached_bytes")
    await blob_storage.retain_media("home-dev", auth_a.image_ref)

    # 2. 建立「未確認孤兒媒體 B」(orphan)
    req_b = UploadAuthorizationRequest(
        household_id="home-dev",
        filename="orphan_item.jpg",
        content_type=AllowedMediaContentType.IMAGE_JPEG,
        file_size_bytes=10,
    )
    auth_b = await blob_storage.create_upload_authorization(req_b)
    token_b = auth_b.upload_url.split("/")[-1]
    await blob_storage.receive_staged_upload(token_b, b"orphan_bytes")

    # 3. 建立「曾於 30 天前建立並保留的舊媒體 C」
    req_c = UploadAuthorizationRequest(
        household_id="home-dev",
        filename="old_retained_item.jpg",
        content_type=AllowedMediaContentType.IMAGE_JPEG,
        file_size_bytes=10,
    )
    auth_c = await blob_storage.create_upload_authorization(req_c)
    token_c = auth_c.upload_url.split("/")[-1]
    await blob_storage.receive_staged_upload(token_c, b"old_bytes")
    await blob_storage.retain_media("home-dev", auth_c.image_ref)

    path_a = auth_a.image_ref.removeprefix("blob://").lstrip("/")
    path_b = auth_b.image_ref.removeprefix("blob://").lstrip("/")
    path_c = auth_c.image_ref.removeprefix("blob://").lstrip("/")

    # 人工將舊媒體 C 的初始建立時間倒退回 30 天前
    now_dt = datetime.now(UTC)
    container._creation_times[path_c] = now_dt - timedelta(days=30)

    # 驗證初始 Tags
    assert (await container.get_blob_client(path_a).get_blob_properties()).tags[
        "retention"
    ] == "attached"
    assert (await container.get_blob_client(path_b).get_blob_properties()).tags[
        "retention"
    ] == "orphan"
    assert (await container.get_blob_client(path_c).get_blob_properties()).tags[
        "retention"
    ] == "attached"

    # 在 30 天後的當下，使用者決定換圖，對舊媒體 C 執行 unretain_media
    unretained = await blob_storage.unretain_media("home-dev", auth_c.image_ref, ttl_seconds=86400)
    assert unretained is True

    # 驗證 unretain_media 後：
    # (a) C 的 tag 成功還原為 orphan
    props_c = await container.get_blob_client(path_c).get_blob_properties()
    assert props_c.tags["retention"] == "orphan"
    # (b) C 的 CreationTime 成功被重設為當前時間（不小於 now_dt）
    assert props_c.creation_time >= now_dt

    # 測試生命週期場景 1：時間推進 12 小時 (未滿 24 小時)
    purged_12h = simulate_azure_lifecycle_policy_purge(current_time=now_dt + timedelta(hours=12))
    assert purged_12h == []  # 無任何檔案被刪除
    assert await container.get_blob_client(path_a).exists() is True
    assert await container.get_blob_client(path_b).exists() is True
    assert await container.get_blob_client(path_c).exists() is True

    # 測試生命週期場景 2：時間推進 25 小時 (滿 24 小時)
    purged_25h = simulate_azure_lifecycle_policy_purge(current_time=now_dt + timedelta(hours=25))
    # 孤立媒體 B 與解除保留後過期的 C 均被正常刪除
    assert path_b in purged_25h
    assert path_c in purged_25h
    # 關鍵斷言：attached 的媒體 A 即使經過 25 小時，依然安在，絕對不會被刪除！
    assert path_a not in purged_25h
    assert await container.get_blob_client(path_a).exists() is True

    # 測試生命週期場景 3：時間推進 30 天
    purged_30d = simulate_azure_lifecycle_policy_purge(current_time=now_dt + timedelta(days=30))
    # 關鍵斷言：attached 的媒體 A 即使經過 30 天，依然永遠不會被刪除！
    assert path_a not in purged_30d
    assert await container.get_blob_client(path_a).exists() is True


@pytest.mark.anyio
async def test_azure_blob_upload_fails_fast_on_hierarchical_namespace(
    blob_storage, fake_blob_service
):
    """驗證當 Azure Storage Account 開啟階層命名空間 (ADLS Gen2) 不支援 Tags 時：

    1. upload_blob 捕獲 FeatureNotYetSupportedForHierarchicalNamespaceAccounts 錯誤。
    2. 適配器落實 Fail-Fast 原則，拋出明確的 RuntimeError，拒絕靜默降級或忽略 Tags。
    3. set_blob_tags 遇到該錯誤時亦拋出 RuntimeError 阻斷。
    """
    del fake_blob_service
    original_upload = FakeBlobClient.upload_blob
    original_set_tags = FakeBlobClient.set_blob_tags

    async def mock_upload_with_tag_failure(self, *args, **kwargs):
        if "tags" in kwargs and kwargs["tags"]:
            raise RuntimeError(
                "Specified feature is not yet supported for hierarchical namespace accounts. "
                "ErrorCode: FeatureNotYetSupportedForHierarchicalNamespaceAccounts"
            )
        return await original_upload(self, *args, **kwargs)

    async def mock_set_tags_with_hns_failure(self, *args, **kwargs):
        raise RuntimeError(
            "Specified feature is not yet supported for hierarchical namespace accounts. "
            "ErrorCode: FeatureNotYetSupportedForHierarchicalNamespaceAccounts"
        )

    FakeBlobClient.upload_blob = mock_upload_with_tag_failure
    FakeBlobClient.set_blob_tags = mock_set_tags_with_hns_failure
    try:
        req = UploadAuthorizationRequest(
            household_id="home-dev",
            filename="adls_test.jpg",
            content_type=AllowedMediaContentType.IMAGE_JPEG,
            file_size_bytes=16,
        )
        auth = await blob_storage.create_upload_authorization(req)
        token = auth.upload_url.split("/")[-1]

        # 斷言：遭遇 HNS 帳戶時強制 Fail-Fast 拋出 RuntimeError，絕不靜默略過
        with pytest.raises(RuntimeError, match="Hierarchical namespace.*not supported"):
            await blob_storage.receive_staged_upload(token, b"0123456789abcdef")

        # 斷言：_set_blob_tags 遭遇 HNS 亦強制 Fail-Fast
        with pytest.raises(RuntimeError, match="Hierarchical namespace.*not supported"):
            await blob_storage._set_blob_tags("some/path.jpg", {"retention": "attached"})
    finally:
        FakeBlobClient.upload_blob = original_upload
        FakeBlobClient.set_blob_tags = original_set_tags


@pytest.mark.anyio
async def test_azure_blob_unretain_preserves_content_settings(
    blob_storage, fake_blob_service
):
    """驗證 PR #155 關鍵要求：

    1. 上傳 image/jpeg 媒體至 Azure Blob。
    2. 驗證初始 content_settings 具有 content_type="image/jpeg"。
    3. 執行 retain_media 推進至 attached。
    4. 執行 unretain_media 解除附加（觸發自我重寫以刷新 24h 建立時間時鐘）。
    5. 斷言重寫後：
       - tags['retention'] 變為 'orphan'
       - content_settings.content_type 依然保持 'image/jpeg'，未被重設或丟失！
    """
    req = UploadAuthorizationRequest(
        household_id="home-dev",
        filename="preserve_mime.jpg",
        content_type=AllowedMediaContentType.IMAGE_JPEG,
        file_size_bytes=32,
    )
    auth = await blob_storage.create_upload_authorization(req)
    token = auth.upload_url.split("/")[-1]

    hh, img_ref = await blob_storage.receive_staged_upload(token, b"X" * 32)
    blob_path = img_ref.removeprefix("blob://").lstrip("/")

    container = fake_blob_service.get_container_client("media")
    blob_client = container.get_blob_client(blob_path)

    # 1. 驗證初始屬性
    init_props = await blob_client.get_blob_properties()
    assert init_props.content_settings.content_type == "image/jpeg"
    init_creation = init_props.creation_time

    # 2. 標記為保留 (attached)
    await blob_storage.retain_media("home-dev", img_ref)
    retained_tags = await blob_client.get_blob_tags()
    assert retained_tags.get("retention") == "attached"

    # 微調時鐘以確保重寫後 creation_time 產生可觀測差異
    time.sleep(0.01)

    # 3. 解除保留 (unretain_media -> orphan, 自我重寫刷新時鐘)
    await blob_storage.unretain_media("home-dev", img_ref)

    # 4. 關鍵斷言：檢查重寫後的 Blob 屬性
    final_props = await blob_client.get_blob_properties()
    final_tags = await blob_client.get_blob_tags()

    assert final_tags.get("retention") == "orphan"
    assert final_props.creation_time >= init_creation
    # 核心不變量：MIME / HTTP Content-Type 必須 100% 完整保留
    assert final_props.content_settings.content_type == "image/jpeg"

"""[A2_PIPELINE_TEST] 驗證 A2 接入 B1 MediaMappingPort 端到端保留流程.

涵蓋驗收條件：
1. 狀態五態流轉 (ALLOCATED -> RECEIVED -> PROCESSING -> COMPLETED / FAILED)
2. Agent completed 門禁 (未完成與跨家庭一律阻斷，唯有 completed 放行)
3. 雜湊校驗 (SHA-256) 與工件持久化 (artifact_ref)
4. Cosmos ETag 樂觀鎖衝突防禦
5. C1/B1 業務同批保留握手 (ttl_seconds=None)
"""

import hashlib
from datetime import UTC, datetime

import pytest

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
from app.services.media_mapping import (
    CosmosMediaMappingStorage,
    build_media_mapping_retention_update,
)
from app.services.provisional_storage.local_staging_adapter import LocalStagingMediaStorage


class FakeCosmosBatchClient:
    """Offline test double simulating Cosmos DB transactional batch."""

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
def fake_client():
    return FakeCosmosBatchClient()


@pytest.fixture
def mapping_storage(fake_client):
    read_repo = CosmosReadRepository(fake_client)
    return CosmosMediaMappingStorage(read_repository=read_repo, write_client=fake_client)


@pytest.fixture
def a2_storage(mapping_storage):
    return LocalStagingMediaStorage(mapping_port=mapping_storage)


@pytest.mark.anyio
async def test_upload_authorization_allocates_mapping(a2_storage, mapping_storage):
    """授權階段建立 ALLOCATED 映射，配置 24 小時孤立 TTL，Agent 尚無法查閱。"""
    req = UploadAuthorizationRequest(
        household_id="home-alpha",
        filename="apple.jpg",
        content_type=AllowedMediaContentType.IMAGE_JPEG,
        file_size_bytes=1024,
    )
    auth = await a2_storage.create_upload_authorization(req)
    assert auth.image_ref.startswith("blob://households/home-alpha/media/")
    assert "upload_url" in auth.model_dump()

    # 查核 Cosmos 映射
    snapshot = await mapping_storage.find_by_image_ref(
        household_id="home-alpha", image_ref=auth.image_ref
    )
    assert snapshot is not None
    assert snapshot.record.status is MediaProcessingStatus.ALLOCATED
    assert snapshot.record.ttl_seconds == 86400
    assert snapshot.record.expected_file_size_bytes == 1024

    # Agent 門禁防禦：ALLOCATED 狀態不可查閱
    can_read = await a2_storage.verify_media_exists("home-alpha", auth.image_ref)
    assert can_read is False


@pytest.mark.anyio
async def test_receive_staged_upload_transitions_to_received(a2_storage, mapping_storage):
    """接收二進位串流推進至 RECEIVED，計算 SHA-256 與檔案大小，Agent 仍無法查閱。"""
    req = UploadAuthorizationRequest(
        household_id="home-alpha",
        filename="milk.jpg",
        content_type=AllowedMediaContentType.IMAGE_JPEG,
        file_size_bytes=4,
    )
    auth = await a2_storage.create_upload_authorization(req)
    token = auth.upload_url.split("/")[-1]

    payload = b"milk"
    sha256_expected = hashlib.sha256(payload).hexdigest()

    hid, img_ref = await a2_storage.receive_staged_upload(token, payload)
    assert hid == "home-alpha"
    assert img_ref == auth.image_ref

    snapshot = await mapping_storage.find_by_image_ref(
        household_id="home-alpha", image_ref=auth.image_ref
    )
    assert snapshot is not None
    assert snapshot.record.status is MediaProcessingStatus.RECEIVED
    assert snapshot.record.file_size_bytes == 4
    assert snapshot.record.sha256_hash == sha256_expected

    # Agent 門禁防禦：RECEIVED 狀態仍不可查閱
    can_read = await a2_storage.verify_media_exists("home-alpha", auth.image_ref)
    assert can_read is False


@pytest.mark.anyio
async def test_full_pipeline_transitions_to_completed_and_agent_unlock(a2_storage, mapping_storage):
    """完整流程：ALLOCATED -> RECEIVED -> PROCESSING -> COMPLETED，工件生成後 Agent 放行。"""
    req = UploadAuthorizationRequest(
        household_id="home-alpha",
        filename="cereal.jpg",
        content_type=AllowedMediaContentType.IMAGE_JPEG,
        file_size_bytes=6,
    )
    auth = await a2_storage.create_upload_authorization(req)
    token = auth.upload_url.split("/")[-1]
    await a2_storage.receive_staged_upload(token, b"cereal")

    # 推進至 PROCESSING
    await a2_storage.begin_processing("home-alpha", auth.image_ref)
    snap_proc = await mapping_storage.find_by_image_ref(
        household_id="home-alpha", image_ref=auth.image_ref
    )
    assert snap_proc.record.status is MediaProcessingStatus.PROCESSING

    # PROCESSING 狀態仍不可查閱
    assert await a2_storage.verify_media_exists("home-alpha", auth.image_ref) is False

    # 模擬推論完成，產出工件並推進至 COMPLETED
    artifact_data = '{"scene_type": "single_item", "detected_items": []}'
    artifact_ref = await a2_storage.complete_processing("home-alpha", auth.image_ref, artifact_data)
    assert "derived/vision_v1.json" in artifact_ref

    snap_comp = await mapping_storage.find_by_image_ref(
        household_id="home-alpha", image_ref=auth.image_ref
    )
    assert snap_comp.record.status is MediaProcessingStatus.COMPLETED
    assert snap_comp.record.artifact_ref == artifact_ref
    assert snap_comp.record.ttl_seconds == 86400  # 尚未附加，維持孤立清理 TTL

    # Agent 門禁放行！
    assert await a2_storage.verify_media_exists("home-alpha", auth.image_ref) is True

    # 跨戶阻斷：不同家庭不可查閱
    assert await a2_storage.verify_media_exists("home-beta", auth.image_ref) is False


@pytest.mark.anyio
async def test_failure_handling_records_reason(a2_storage, mapping_storage):
    """異常或超時推進至 FAILED，記錄 failure_reason，維持 24h 孤立焚燬，Agent 阻斷。"""
    req = UploadAuthorizationRequest(
        household_id="home-alpha",
        filename="corrupt.jpg",
        content_type=AllowedMediaContentType.IMAGE_JPEG,
        file_size_bytes=7,
    )
    auth = await a2_storage.create_upload_authorization(req)
    token = auth.upload_url.split("/")[-1]
    await a2_storage.receive_staged_upload(token, b"corrupt")
    await a2_storage.begin_processing("home-alpha", auth.image_ref)

    # 標記失敗
    await a2_storage.fail_processing(
        "home-alpha", auth.image_ref, MediaFailureReason.PROCESSING_FAILED
    )

    snap_fail = await mapping_storage.find_by_image_ref(
        household_id="home-alpha", image_ref=auth.image_ref
    )
    assert snap_fail.record.status is MediaProcessingStatus.FAILED
    assert snap_fail.record.failure_reason == MediaFailureReason.PROCESSING_FAILED
    assert snap_fail.record.ttl_seconds == 86400

    # Agent 門禁阻斷
    assert await a2_storage.verify_media_exists("home-alpha", auth.image_ref) is False


@pytest.mark.anyio
async def test_retention_handshake_removes_ttl(a2_storage, mapping_storage, fake_client):
    """業務實體附加保留握手：透過 build_media_mapping_retention_update 清除 TTL 永久保留。"""
    req = UploadAuthorizationRequest(
        household_id="home-alpha",
        filename="jam.jpg",
        content_type=AllowedMediaContentType.IMAGE_JPEG,
        file_size_bytes=3,
    )
    auth = await a2_storage.create_upload_authorization(req)
    token = auth.upload_url.split("/")[-1]
    await a2_storage.receive_staged_upload(token, b"jam")
    await a2_storage.begin_processing("home-alpha", auth.image_ref)
    await a2_storage.complete_processing("home-alpha", auth.image_ref, "{}")

    # 取得 completed snapshot
    snapshot = await a2_storage.get_media_snapshot("home-alpha", auth.image_ref)
    assert snapshot is not None
    assert snapshot.record.status is MediaProcessingStatus.COMPLETED

    # 業務附加操作 (C1 / B1 同批 TransactionalBatch)
    retention_op = build_media_mapping_retention_update(snapshot, ttl_seconds=None)
    await fake_client.execute_batch(
        partition_key="home-alpha",
        operations=(retention_op,),
    )

    # 查驗 TTL 已被清除
    retained_snap = await mapping_storage.find_by_image_ref(
        household_id="home-alpha", image_ref=auth.image_ref
    )
    assert retained_snap.record.ttl_seconds is None  # 永久保留
    assert retained_snap.record.status is MediaProcessingStatus.COMPLETED


@pytest.mark.anyio
async def test_etag_conflict_on_concurrent_transition(a2_storage, mapping_storage):
    """ETag 樂觀鎖並發衝突防禦：過期 ETag 轉移引發 BatchOperationConflict。"""
    req = UploadAuthorizationRequest(
        household_id="home-alpha",
        filename="bread.jpg",
        content_type=AllowedMediaContentType.IMAGE_JPEG,
        file_size_bytes=5,
    )
    auth = await a2_storage.create_upload_authorization(req)
    token = auth.upload_url.split("/")[-1]
    await a2_storage.receive_staged_upload(token, b"bread")

    # 取得當前 snapshot
    snap1 = await a2_storage.get_media_snapshot("home-alpha", auth.image_ref)

    # 執行一次推進，變更 Cosmos ETag
    await a2_storage.begin_processing("home-alpha", auth.image_ref)

    # 以過期的 snap1.etag 嘗試轉移，必須被 Cosmos 樂觀鎖阻斷
    with pytest.raises(BatchOperationConflict):
        await mapping_storage.transition(
            household_id="home-alpha",
            input_id=snap1.record.input_id,
            expected_etag=snap1.etag,  # 過期 ETag
            status=MediaProcessingStatus.COMPLETED,
            updated_at=datetime.now(UTC),
            artifact_ref="blob://invalid",
        )


@pytest.mark.anyio
async def test_read_agent_integration_gate(a2_storage):
    """驗證 A1 ReadAgent 門禁：未 completed 拋 PHOTO_NOT_FOUND，completed 正常放行。"""
    from langchain_core.messages import AIMessage

    from app.agent.read_agent import PHOTO_NOT_FOUND, ReadAgent, load_system_prompt
    from app.core.dependencies import get_inventory_service, get_shopping_service
    from app.models.agent import AgentRequest
    from app.services.vision import MockVisionService
    from app.tools.queries import QueryTools

    class DummyModel:
        def __init__(self):
            self.seen = []
        def bind_tools(self, tools):
            return self
        async def ainvoke(self, messages, config=None):
            self.seen.append(messages)
            return AIMessage(content="看到照片內容。")

    req = UploadAuthorizationRequest(
        household_id="home-alpha",
        filename="soup.jpg",
        content_type=AllowedMediaContentType.IMAGE_JPEG,
        file_size_bytes=4,
    )
    auth = await a2_storage.create_upload_authorization(req)
    token = auth.upload_url.split("/")[-1]
    await a2_storage.receive_staged_upload(token, b"soup")

    tools = QueryTools(get_inventory_service(), get_shopping_service())
    agent = ReadAgent(
        DummyModel(),
        tools,
        load_system_prompt(),
        media=a2_storage,
        vision=MockVisionService(),
    )

    # 1. RECEIVED 階段，Agent 呼叫直接阻斷為 PHOTO_NOT_FOUND
    res_blocked = await agent.respond(
        AgentRequest(household_id="home-alpha", message="辨識這個", image_ref=auth.image_ref)
    )
    assert res_blocked.message == PHOTO_NOT_FOUND

    # 2. 跨戶查閱，直接阻斷為 PHOTO_NOT_FOUND
    res_cross = await agent.respond(
        AgentRequest(household_id="home-beta", message="辨識這個", image_ref=auth.image_ref)
    )
    assert res_cross.message == PHOTO_NOT_FOUND

    # 3. 推進至 COMPLETED 階段
    await a2_storage.begin_processing("home-alpha", auth.image_ref)
    await a2_storage.complete_processing("home-alpha", auth.image_ref, "{}")

    # 4. COMPLETED 階段，Agent 門禁放行
    res_allowed = await agent.respond(
        AgentRequest(household_id="home-alpha", message="辨識這個", image_ref=auth.image_ref)
    )
    assert res_allowed.message != PHOTO_NOT_FOUND
    assert "看到照片內容" in res_allowed.message


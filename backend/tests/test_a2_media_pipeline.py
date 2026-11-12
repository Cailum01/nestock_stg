"""[A2_PIPELINE_TEST] 驗證 A2 接入 B1 MediaMappingPort 端到端保留流程.

涵蓋驗收條件：
1. 狀態五態流轉 (ALLOCATED -> RECEIVED -> PROCESSING -> COMPLETED / FAILED)
2. Agent 就緒門禁 (未上傳 ALLOCATED 與跨家庭一律阻斷，RECEIVED/COMPLETED 放行)
3. 雜湊校驗 (SHA-256) 與工件持久化 (artifact_ref)
4. Cosmos ETag 樂觀鎖衝突防禦
5. 上傳超時 (UPLOAD_TIMEOUT) 與驗證失敗 (VALIDATION_FAILED) 轉移至 FAILED
6. C1/B1 業務同批保留握手 (ttl_seconds=None)
"""

import hashlib
import time
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
    return LocalStagingMediaStorage(mapping_port=mapping_storage, auto_process=False)


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

    # Agent 門禁防禦：未上傳前 (ALLOCATED) 嚴格阻斷
    can_read = await a2_storage.verify_media_exists("home-alpha", auth.image_ref)
    assert can_read is False


@pytest.mark.anyio
async def test_receive_staged_upload_transitions_to_received(a2_storage, mapping_storage):
    """接收二進位串流推進至 RECEIVED，計算 SHA-256 與檔案大小，Agent 就緒放行。"""
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

    # Agent 門禁驗證：依據 Issue #98 與設計文件 §8.2，未處理完成前 (RECEIVED) 嚴格阻斷
    can_read = await a2_storage.verify_media_exists("home-alpha", auth.image_ref)
    assert can_read is False


@pytest.mark.anyio
async def test_full_pipeline_transitions_to_completed_and_agent_unlock(a2_storage, mapping_storage):
    """完整流程：ALLOCATED -> RECEIVED -> PROCESSING -> COMPLETED，工件生成後放行。"""
    req = UploadAuthorizationRequest(
        household_id="home-alpha",
        filename="cereal.jpg",
        content_type=AllowedMediaContentType.IMAGE_JPEG,
        file_size_bytes=6,
    )
    auth = await a2_storage.create_upload_authorization(req)
    token = auth.upload_url.split("/")[-1]

    # ALLOCATED 階段：Agent 阻斷
    assert await a2_storage.verify_media_exists("home-alpha", auth.image_ref) is False

    await a2_storage.receive_staged_upload(token, b"cereal")

    # RECEIVED 階段：未完成處理前，Agent 門禁嚴格阻斷
    assert await a2_storage.verify_media_exists("home-alpha", auth.image_ref) is False

    # 推進至 PROCESSING
    await a2_storage.begin_processing("home-alpha", auth.image_ref)
    snap_proc = await mapping_storage.find_by_image_ref(
        household_id="home-alpha", image_ref=auth.image_ref
    )
    assert snap_proc.record.status is MediaProcessingStatus.PROCESSING

    # PROCESSING 階段：處理中尚未完成，Agent 門禁嚴格阻斷
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

    # COMPLETED 階段放行
    assert await a2_storage.verify_media_exists("home-alpha", auth.image_ref) is True

    # 跨戶阻斷：不同家庭不可查閱
    assert await a2_storage.verify_media_exists("home-beta", auth.image_ref) is False


@pytest.mark.anyio
async def test_upload_timeout_transitions_to_failed(a2_storage, mapping_storage):
    """上傳逾時推進至 FAILED，記錄 UPLOAD_TIMEOUT 原因，Agent 門禁阻斷。"""
    req = UploadAuthorizationRequest(
        household_id="home-alpha",
        filename="slow.jpg",
        content_type=AllowedMediaContentType.IMAGE_JPEG,
        file_size_bytes=5,
    )
    auth = await a2_storage.create_upload_authorization(req)
    token = auth.upload_url.split("/")[-1]

    # 人為模擬 token 逾時
    a2_storage._pending_tokens[token]["expires_at"] = time.time() - 1

    with pytest.raises(TimeoutError):
        await a2_storage.receive_staged_upload(token, b"slow_data")

    snap_fail = await mapping_storage.find_by_image_ref(
        household_id="home-alpha", image_ref=auth.image_ref
    )
    assert snap_fail is not None
    assert snap_fail.record.status is MediaProcessingStatus.FAILED
    assert snap_fail.record.failure_reason == MediaFailureReason.UPLOAD_TIMEOUT
    assert snap_fail.record.ttl_seconds == 86400

    # Agent 門禁阻斷
    assert await a2_storage.verify_media_exists("home-alpha", auth.image_ref) is False


@pytest.mark.anyio
async def test_upload_validation_failure_transitions_to_failed(a2_storage, mapping_storage):
    """上傳空 payload 或超限推進至 FAILED，記錄 VALIDATION_FAILED 原因。"""
    req = UploadAuthorizationRequest(
        household_id="home-alpha",
        filename="empty.jpg",
        content_type=AllowedMediaContentType.IMAGE_JPEG,
        file_size_bytes=100,
    )
    auth = await a2_storage.create_upload_authorization(req)
    token = auth.upload_url.split("/")[-1]

    # 空 payload 驗證失敗
    with pytest.raises(ValueError, match="empty"):
        await a2_storage.receive_staged_upload(token, b"")

    snap_fail = await mapping_storage.find_by_image_ref(
        household_id="home-alpha", image_ref=auth.image_ref
    )
    assert snap_fail is not None
    assert snap_fail.record.status is MediaProcessingStatus.FAILED
    assert snap_fail.record.failure_reason == MediaFailureReason.VALIDATION_FAILED

    # Agent 門禁阻斷
    assert await a2_storage.verify_media_exists("home-alpha", auth.image_ref) is False


@pytest.mark.anyio
async def test_failure_handling_records_reason(a2_storage, mapping_storage):
    """處理失敗推進至 FAILED，記錄 PROCESSING_FAILED，維持 24h 孤立焚燬，Agent 阻斷。"""
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
async def test_read_agent_integration_gate(mapping_storage):
    """驗證 A1 ReadAgent 門禁與 Ingest 管線時序：
    未上傳阻斷，上傳完成自動 COMPLETED 且 Agent 放行。
    """
    from langchain_core.messages import AIMessage

    from app.agent.read_agent import PHOTO_NOT_FOUND, ReadAgent, load_system_prompt
    from app.core.dependencies import get_inventory_service, get_shopping_service
    from app.models.agent import AgentRequest
    from app.models.vision import DetectedItem, SceneType, VisionAnalysisResult
    from app.services.vision import VisionService
    from app.tools.queries import QueryTools

    # 使用真實執行期配置：啟用 auto_process Ingest Pipeline
    runtime_storage = LocalStagingMediaStorage(mapping_port=mapping_storage, auto_process=True)

    class PipelineTrackingVisionService(VisionService):
        def __init__(self, storage):
            self.storage = storage
            self.called = False

        async def analyze_image(self, household_id, image_ref, scene_type=None):
            self.called = True
            await self.storage.begin_processing(household_id, image_ref)
            result = VisionAnalysisResult(
                image_ref=image_ref,
                scene_type=SceneType.SINGLE_ITEM,
                detected_items=[
                    DetectedItem(
                        raw_detected_name="雞湯",
                        canonical_name="雞湯",
                        quantity=1,
                        unit="罐",
                        confidence=0.95,
                    )
                ],
            )
            await self.storage.complete_processing(
                household_id, image_ref, result.model_dump_json()
            )
            return result

    class DummyModel:
        def __init__(self, *replies):
            self.replies = list(replies)
            self.seen = []

        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages, config=None):
            self.seen.append(messages)
            if self.replies:
                return self.replies.pop(0)
            return AIMessage(content="看到照片內容：雞湯 1 罐。")

    req = UploadAuthorizationRequest(
        household_id="home-alpha",
        filename="soup.jpg",
        content_type=AllowedMediaContentType.IMAGE_JPEG,
        file_size_bytes=4,
    )
    auth = await runtime_storage.create_upload_authorization(req)
    token = auth.upload_url.split("/")[-1]

    vision_service = PipelineTrackingVisionService(runtime_storage)
    tools = QueryTools(get_inventory_service(), get_shopping_service())
    model = DummyModel(
        AIMessage(content="", tool_calls=[{"name": "analyze_photo", "args": {}, "id": "call-1"}]),
        AIMessage(content="看到照片內容：雞湯 1 罐。"),
    )
    agent = ReadAgent(
        model,
        tools,
        load_system_prompt(),
        media=runtime_storage,
        vision=vision_service,
    )

    # 1. ALLOCATED 階段 (尚未上傳 bytes)，Agent 呼叫直接阻斷為 PHOTO_NOT_FOUND
    res_not_uploaded = await agent.respond(
        AgentRequest(household_id="home-alpha", message="辨識這個", image_ref=auth.image_ref)
    )
    assert res_not_uploaded.message == PHOTO_NOT_FOUND

    # 2. 跨戶查閱，直接阻斷為 PHOTO_NOT_FOUND
    res_cross = await agent.respond(
        AgentRequest(household_id="home-beta", message="辨識這個", image_ref=auth.image_ref)
    )
    assert res_cross.message == PHOTO_NOT_FOUND

    # 3. 完成 PUT 上傳二進位檔案，Ingest 管線自動推進至 COMPLETED
    await runtime_storage.receive_staged_upload(token, b"soup")

    # 4. 真實使用者請求路徑：COMPLETED 階段呼叫 Agent，門禁放行且自然消費推論結果
    res_allowed = await agent.respond(
        AgentRequest(household_id="home-alpha", message="辨識這個", image_ref=auth.image_ref)
    )
    assert res_allowed.message != PHOTO_NOT_FOUND
    assert "看到照片內容" in res_allowed.message

    # 5. 驗證 Cosmos mapping 已由真實 pipeline 自然推進至 COMPLETED
    snap_final = await mapping_storage.find_by_image_ref(
        household_id="home-alpha", image_ref=auth.image_ref
    )
    assert snap_final.record.status is MediaProcessingStatus.COMPLETED
    assert "derived/vision_v1.json" in snap_final.record.artifact_ref


@pytest.mark.anyio
async def test_fail_processing_error_propagation_on_timeout(mapping_storage):
    """驗證上傳逾時若 FAILED 狀態寫入失敗，例外會明確向上拋出，不再被靜默吞噬。"""
    storage = LocalStagingMediaStorage(mapping_port=mapping_storage)
    req = UploadAuthorizationRequest(
        household_id="home-alpha",
        filename="broken.jpg",
        content_type=AllowedMediaContentType.IMAGE_JPEG,
        file_size_bytes=10,
    )
    auth = await storage.create_upload_authorization(req)
    token = auth.upload_url.split("/")[-1]

    # 人為模擬 token 逾時
    storage._pending_tokens[token]["expires_at"] = time.time() - 1

    # 模擬 fail_processing 遭遇 Cosmos 錯誤
    async def mock_fail_error(*args, **kwargs):
        raise RuntimeError("Cosmos DB write failure during fail_processing")

    storage.fail_processing = mock_fail_error

    with pytest.raises(RuntimeError, match="Cosmos DB write failure"):
        await storage.receive_staged_upload(token, b"broken_data")


@pytest.mark.anyio
async def test_verify_media_exists_propagates_database_error(mapping_storage):
    """驗證 Cosmos DB 查詢異常時，verify_media_exists 嚴格向外傳播，不偽裝為 False。"""
    storage = LocalStagingMediaStorage(mapping_port=mapping_storage)

    # 模擬 find_completed_by_image_ref 拋出網路或連線崩潰
    async def mock_query_error(*args, **kwargs):
        raise ConnectionResetError("Cosmos DB connection reset")

    mapping_storage.find_completed_by_image_ref = mock_query_error

    with pytest.raises(ConnectionResetError, match="Cosmos DB connection reset"):
        await storage.verify_media_exists(
            "home-alpha", "blob://households/home-alpha/media/test.jpg"
        )


@pytest.mark.anyio
async def test_analyze_image_idempotency_on_completed_photo(mapping_storage):
    """驗證已完成之照片多次調用 analyze_image 時，能安全等冪復用且不引發狀態機轉移衝突。"""
    from app.services.vision import AzureOpenAIVisionService, VisionOpenAISettings

    storage = LocalStagingMediaStorage(mapping_port=mapping_storage, auto_process=True)
    req = UploadAuthorizationRequest(
        household_id="home-alpha",
        filename="repeat.jpg",
        content_type=AllowedMediaContentType.IMAGE_JPEG,
        file_size_bytes=6,
    )
    auth = await storage.create_upload_authorization(req)
    token = auth.upload_url.split("/")[-1]

    # 上傳並自動推進至 COMPLETED
    await storage.receive_staged_upload(token, b"repeat")

    settings = VisionOpenAISettings(
        endpoint="https://mock-vision.openai.azure.com/",
        api_key="mock-key",
        deployment="gpt-4o",
    )
    vision_service = AzureOpenAIVisionService(
        settings=settings, media_storage=storage, mock_fallback=True
    )

    # 第一次調用：成功取得分析結果
    res1 = await vision_service.analyze_image("home-alpha", auth.image_ref)
    assert res1.image_ref == auth.image_ref

    # 第二次調用：應安全復用快取，絕不可引發 COMPLETED -> PROCESSING 轉移例外
    res2 = await vision_service.analyze_image("home-alpha", auth.image_ref)
    assert res2.image_ref == auth.image_ref
    assert res2.detected_items == res1.detected_items

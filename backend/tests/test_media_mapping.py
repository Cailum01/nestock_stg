import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.models.domain import (
    MediaFailureReason,
    MediaInputMapping,
    MediaProcessingStatus,
)
from app.services.cosmos_read import CosmosDocumentMapper, CosmosReadRepository
from app.services.cosmos_write import BatchOperationConflict, CreateDocument, ReplaceDocument
from app.services.media_mapping import (
    CosmosMediaMappingStorage,
    MediaMappingConflict,
    MediaMappingNotReady,
    build_media_mapping_retention_update,
)

NOW = datetime(2026, 11, 11, tzinfo=UTC)
SHA256 = "a" * 64


class FakeMediaMappingClient:
    def __init__(self):
        self.documents = {}
        self.queries = []
        self.batches = []
        self._etag_counter = 0
        self.timeout_after_commit = False

    async def query_items(self, *, query, parameters, partition_key):
        self.queries.append(
            {"query": query, "parameters": tuple(parameters), "partition_key": partition_key}
        )
        values = {parameter["name"]: parameter["value"] for parameter in parameters}
        return [
            dict(document)
            for (household_id, _), document in self.documents.items()
            if household_id == partition_key
            and document.get("record_type") == values.get("@record_type")
            and document.get("household_id") == values.get("@household_id")
            and document.get("image_ref") == values.get("@image_ref")
        ]

    async def read_item(self, *, item_id, partition_key):
        document = self.documents.get((partition_key, item_id))
        return None if document is None else dict(document)

    async def execute_batch(self, *, partition_key, operations):
        self.batches.append({"partition_key": partition_key, "operations": tuple(operations)})
        staged = {}
        next_etag = self._etag_counter
        for index, operation in enumerate(operations):
            document = dict(operation.document)
            if document.get("household_id") != partition_key:
                raise ValueError("Batch document does not match the household partition.")
            key = (partition_key, document["id"])
            if isinstance(operation, CreateDocument):
                if key in self.documents or key in staged:
                    raise BatchOperationConflict(index)
            elif isinstance(operation, ReplaceDocument):
                current = self.documents.get(key)
                if current is None or current.get("_etag") != operation.if_match_etag:
                    raise BatchOperationConflict(index)
            next_etag += 1
            document["_etag"] = f'"etag-{next_etag}"'
            staged[key] = document

        self.documents.update(staged)
        self._etag_counter = next_etag
        if self.timeout_after_commit:
            self.timeout_after_commit = False
            raise TimeoutError("Simulated lost response after committed batch.")


def mapping(
    *,
    input_id="input-1",
    household_id="home",
    image_ref="opaque://media-1",
    status=MediaProcessingStatus.ALLOCATED,
    **updates,
):
    values = {
        "input_id": input_id,
        "household_id": household_id,
        "image_ref": image_ref,
        "content_type": "image/jpeg",
        "expected_file_size_bytes": 100,
        "status": status,
        "upload_authorization_expires_at": NOW + timedelta(minutes=5),
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(updates)
    return MediaInputMapping.model_validate(values)


def make_storage(client=None):
    client = client or FakeMediaMappingClient()
    return CosmosMediaMappingStorage(
        read_repository=CosmosReadRepository(client),
        write_client=client,
    ), client


def create(storage, record=None, *, orphan_ttl_seconds=86_400):
    return asyncio.run(
        storage.create_mapping(
            record or mapping(),
            orphan_ttl_seconds=orphan_ttl_seconds,
        )
    )


def test_mapping_document_round_trip_uses_internal_namespaced_id_and_ttl():
    record = mapping(ttl_seconds=86_400)

    document = CosmosDocumentMapper.to_document(record)

    assert document["id"] == "input:input-1"
    assert document["record_type"] == "user_input"
    assert document["image_ref"] == "opaque://media-1"
    assert document["ttl"] == 86_400
    assert "ttl_seconds" not in document
    assert (
        CosmosDocumentMapper.from_document(
            MediaInputMapping,
            document | {"_etag": '"etag-1"'},
            household_id="home",
        )
        == record
    )


def test_create_is_household_scoped_and_idempotent_for_the_same_mapping():
    storage, client = make_storage()

    first = create(storage)
    retry = create(storage)

    assert retry == first
    assert len(client.documents) == 1
    assert len(client.batches) == 2
    assert client.batches[0]["partition_key"] == "home"
    stored = client.documents[("home", "input:input-1")]
    assert stored["ttl"] == 86_400
    assert "image_bytes" not in stored


def test_create_rejects_reused_input_id_with_different_mapping():
    storage, _ = make_storage()
    create(storage)

    with pytest.raises(MediaMappingConflict, match="already maps to different values"):
        create(storage, mapping(image_ref="opaque://different"))


def test_lookup_uses_exact_opaque_reference_and_household_partition():
    storage, client = make_storage()
    create(storage)
    create(storage, mapping(input_id="input-2", household_id="other"))

    found = asyncio.run(
        storage.find_by_image_ref(household_id="home", image_ref="opaque://media-1")
    )
    missing = asyncio.run(
        storage.find_by_image_ref(household_id="unknown", image_ref="opaque://media-1")
    )

    assert found is not None
    assert found.record.input_id == "input-1"
    assert missing is None
    assert [query["partition_key"] for query in client.queries] == ["home", "unknown"]
    assert all("image_ref = @image_ref" in query["query"] for query in client.queries)


def test_noncompleted_mapping_is_not_usable_by_agent():
    storage, _ = make_storage()
    create(storage)

    with pytest.raises(MediaMappingNotReady, match="Only completed"):
        asyncio.run(
            storage.find_completed_by_image_ref(
                household_id="home",
                image_ref="opaque://media-1",
            )
        )


def test_lifecycle_transitions_and_lost_response_retry():
    storage, client = make_storage()
    allocated = create(storage)
    client.timeout_after_commit = True

    received_args = {
        "household_id": "home",
        "input_id": "input-1",
        "expected_etag": allocated.etag,
        "status": MediaProcessingStatus.RECEIVED,
        "updated_at": NOW + timedelta(minutes=1),
        "file_size_bytes": 100,
        "sha256_hash": SHA256,
    }
    with pytest.raises(TimeoutError, match="lost response"):
        asyncio.run(storage.transition(**received_args))

    received = asyncio.run(storage.transition(**received_args))
    processing = asyncio.run(
        storage.transition(
            household_id="home",
            input_id="input-1",
            expected_etag=received.etag,
            status=MediaProcessingStatus.PROCESSING,
            updated_at=NOW + timedelta(minutes=2),
        )
    )
    completed = asyncio.run(
        storage.transition(
            household_id="home",
            input_id="input-1",
            expected_etag=processing.etag,
            status=MediaProcessingStatus.COMPLETED,
            updated_at=NOW + timedelta(minutes=3),
            artifact_ref="artifact://bounded-observation",
        )
    )
    ready = asyncio.run(
        storage.find_completed_by_image_ref(
            household_id="home",
            image_ref="opaque://media-1",
        )
    )

    assert received.record.status is MediaProcessingStatus.RECEIVED
    assert processing.record.status is MediaProcessingStatus.PROCESSING
    assert completed.record.status is MediaProcessingStatus.COMPLETED
    assert ready == completed
    assert completed.record.ttl_seconds == 86_400


def test_state_transition_requires_metadata_and_rejects_skipped_states():
    storage, _ = make_storage()
    allocated = create(storage)

    with pytest.raises(ValueError, match="requires file_size_bytes and sha256_hash"):
        asyncio.run(
            storage.transition(
                household_id="home",
                input_id="input-1",
                expected_etag=allocated.etag,
                status=MediaProcessingStatus.RECEIVED,
                updated_at=NOW + timedelta(minutes=1),
            )
        )
    with pytest.raises(MediaMappingConflict, match="Cannot transition"):
        asyncio.run(
            storage.transition(
                household_id="home",
                input_id="input-1",
                expected_etag=allocated.etag,
                status=MediaProcessingStatus.COMPLETED,
                updated_at=NOW + timedelta(minutes=1),
                artifact_ref="artifact://bounded-observation",
            )
        )


def test_stale_etag_does_not_overwrite_a_concurrent_transition():
    storage, _ = make_storage()
    allocated = create(storage)
    asyncio.run(
        storage.transition(
            household_id="home",
            input_id="input-1",
            expected_etag=allocated.etag,
            status=MediaProcessingStatus.RECEIVED,
            updated_at=NOW + timedelta(minutes=1),
            file_size_bytes=100,
            sha256_hash=SHA256,
        )
    )

    with pytest.raises(BatchOperationConflict):
        asyncio.run(
            storage.transition(
                household_id="home",
                input_id="input-1",
                expected_etag=allocated.etag,
                status=MediaProcessingStatus.FAILED,
                updated_at=NOW + timedelta(minutes=2),
                failure_reason=MediaFailureReason.PROCESSING_FAILED,
            )
        )


def test_retention_update_is_an_etag_replace_for_same_household_batch():
    storage, client = make_storage()
    created = create(storage)

    operation = build_media_mapping_retention_update(created, ttl_seconds=None)
    asyncio.run(
        client.execute_batch(
            partition_key="home",
            operations=(operation,),
        )
    )
    retained = asyncio.run(
        storage.find_by_image_ref(household_id="home", image_ref="opaque://media-1")
    )

    assert operation.if_match_etag == created.etag
    assert "ttl" not in operation.document
    assert retained is not None
    assert retained.record.ttl_seconds is None

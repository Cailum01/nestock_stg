import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.services.cosmos_write import (
    BatchOperationConflict,
    CosmosWriteRepository,
    CreateDocument,
    OperationIdConflict,
    WriteStatus,
)
from app.services.media_retention_work import (
    CosmosMediaRetentionWorkItemStorage,
    MediaRetentionWorkItemConflict,
    MediaRetentionWorkStatus,
    build_pending_media_retention_work_item,
)

NOW = datetime(2026, 11, 17, 8, 0, tzinfo=UTC)


class FakeCosmos:
    def __init__(self):
        self.documents = {}
        self.batches = []
        self.queries = []
        self._etag_counter = 0
        self.timeout_after_commit = False

    async def execute_batch(self, *, partition_key, operations):
        operations = tuple(operations)
        self.batches.append({"partition_key": partition_key, "operations": operations})
        staged = {}
        for index, operation in enumerate(operations):
            document = dict(operation.document)
            if document.get("household_id") != partition_key:
                raise ValueError("Batch document does not match the household partition.")
            key = (partition_key, document["id"])
            if isinstance(operation, CreateDocument):
                if key in self.documents or key in staged:
                    raise BatchOperationConflict(index)
            else:
                current = self.documents.get(key)
                if current is None or current.get("_etag") != operation.if_match_etag:
                    raise BatchOperationConflict(index)
            self._etag_counter += 1
            document["_etag"] = f'"etag-{self._etag_counter}"'
            staged[key] = document
        self.documents.update(staged)
        if self.timeout_after_commit:
            self.timeout_after_commit = False
            raise TimeoutError("Simulated lost response after committed batch.")

    async def read_item(self, *, item_id, partition_key):
        document = self.documents.get((partition_key, item_id))
        return None if document is None else dict(document)

    async def query_items(self, *, query, parameters, partition_key):
        self.queries.append(
            {"query": query, "parameters": tuple(parameters), "partition_key": partition_key}
        )
        values = {parameter["name"]: parameter["value"] for parameter in parameters}
        due = [
            dict(document)
            for (household_id, _), document in self.documents.items()
            if household_id == partition_key
            and document.get("record_type") == values["@record_type"]
            and document.get("household_id") == values["@household_id"]
            and document.get("status") == values["@status"]
            and document.get("next_attempt_at") <= values["@now"]
        ]
        due.sort(key=lambda document: (document["next_attempt_at"], document["created_at"]))
        return due[: values["@limit"]]


def build_work_item(
    *,
    household_id="home",
    operation_id="operation-1",
    approval_id="approval-1",
    image_ref="opaque://image-1",
    created_at=NOW,
):
    return build_pending_media_retention_work_item(
        household_id=household_id,
        operation_id=operation_id,
        approval_id=approval_id,
        image_ref=image_ref,
        created_at=created_at,
    )


def storage_for(client=None):
    client = client or FakeCosmos()
    return CosmosMediaRetentionWorkItemStorage(
        write_client=client,
        query_client=client,
    ), client


def test_work_item_document_has_stable_identity_and_no_ttl():
    operation = build_work_item()

    document = operation.document

    assert document["id"].startswith("media_retention:")
    assert document["record_type"] == "media_retention_work_item"
    assert document["household_id"] == "home"
    assert document["operation_id"] == "operation-1"
    assert document["approval_id"] == "approval-1"
    assert document["image_ref"] == "opaque://image-1"
    assert document["status"] == "pending"
    assert document["attempt_count"] == 0
    assert document["next_attempt_at"] == "2026-11-17T08:00:00.000000Z"
    assert document["last_error_code"] is None
    assert document["created_at"] == document["updated_at"]
    assert "ttl" not in document
    assert build_work_item(image_ref="opaque://other").document["id"] == document["id"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("household_id", ""),
        ("operation_id", ""),
        ("approval_id", ""),
        ("image_ref", ""),
    ],
)
def test_work_item_creation_rejects_empty_identity(field, value):
    values = {
        "household_id": "home",
        "operation_id": "operation-1",
        "approval_id": "approval-1",
        "image_ref": "opaque://image-1",
        "created_at": NOW,
    }
    values[field] = value

    with pytest.raises(ValueError):
        build_pending_media_retention_work_item(**values)


def test_work_item_creation_rejects_naive_timestamp():
    with pytest.raises(ValueError, match="timezone-aware"):
        build_work_item(created_at=datetime(2026, 11, 17))


def test_idempotent_household_batch_can_include_domain_and_work_item_documents():
    client = FakeCosmos()
    repository = CosmosWriteRepository(client, query_client=client)
    work_item = build_work_item()
    domain_document = {
        "id": "inventory_item:item-1",
        "record_type": "inventory_item",
        "household_id": "home",
        "item_id": "item-1",
    }

    result = asyncio.run(
        repository.execute_idempotent_batch(
            household_id="home",
            operation_id="operation-1",
            operations=(CreateDocument(domain_document), work_item),
        )
    )
    retry = asyncio.run(
        repository.execute_idempotent_batch(
            household_id="home",
            operation_id="operation-1",
            operations=(CreateDocument(domain_document), work_item),
        )
    )

    assert result is WriteStatus.APPLIED
    assert retry is WriteStatus.ALREADY_APPLIED
    assert client.batches[0]["partition_key"] == "home"
    assert len(client.batches[0]["operations"]) == 3
    assert len(client.documents) == 3


def test_idempotent_batch_rejects_reused_operation_with_changed_work_item():
    client = FakeCosmos()
    repository = CosmosWriteRepository(client, query_client=client)
    domain_document = {
        "id": "inventory_item:item-1",
        "record_type": "inventory_item",
        "household_id": "home",
        "item_id": "item-1",
    }
    original = build_work_item()
    asyncio.run(
        repository.execute_idempotent_batch(
            household_id="home",
            operation_id="operation-1",
            operations=(CreateDocument(domain_document), original),
        )
    )

    with pytest.raises(OperationIdConflict):
        asyncio.run(
            repository.execute_idempotent_batch(
                household_id="home",
                operation_id="operation-1",
                operations=(
                    CreateDocument(domain_document),
                    build_work_item(image_ref="opaque://different-image"),
                ),
            )
        )


def test_list_due_is_household_scoped_ordered_and_bounded():
    storage, client = storage_for()
    first = build_work_item(operation_id="operation-1")
    later = build_work_item(
        operation_id="operation-2",
        created_at=NOW + timedelta(minutes=5),
    )
    other_household = build_work_item(
        household_id="other",
        operation_id="operation-3",
    )
    for operation in (first, later, other_household):
        asyncio.run(
            client.execute_batch(
                partition_key=operation.document["household_id"],
                operations=(operation,),
            )
        )

    result = asyncio.run(storage.list_due(household_id="home", now=NOW, limit=1))

    assert [item.record.operation_id for item in result] == ["operation-1"]
    assert client.queries[0]["partition_key"] == "home"
    assert {"name": "@limit", "value": 1} in client.queries[0]["parameters"]
    assert "c.next_attempt_at <= @now" in client.queries[0]["query"]


@pytest.mark.parametrize("limit", [0, 101, True])
def test_list_due_rejects_invalid_limit(limit):
    storage, _ = storage_for()

    with pytest.raises(ValueError, match="limit"):
        asyncio.run(storage.list_due(household_id="home", now=NOW, limit=limit))


def test_transient_failure_reschedules_pending_and_completion_is_idempotent():
    storage, client = storage_for()
    operation = build_work_item()
    asyncio.run(client.execute_batch(partition_key="home", operations=(operation,)))
    initial = asyncio.run(storage.get(household_id="home", operation_id="operation-1"))
    assert initial is not None

    retry_at = NOW + timedelta(minutes=2)
    pending = asyncio.run(
        storage.transition(
            initial,
            status=MediaRetentionWorkStatus.PENDING,
            attempt_count=1,
            next_attempt_at=retry_at,
            last_error_code="http_503",
            updated_at=NOW + timedelta(seconds=1),
        )
    )
    assert pending.record.attempt_count == 1
    assert pending.record.next_attempt_at == retry_at
    assert pending.record.last_error_code == "http_503"

    completed = asyncio.run(
        storage.transition(
            pending,
            status=MediaRetentionWorkStatus.COMPLETED,
            attempt_count=2,
            next_attempt_at=None,
            last_error_code=None,
            updated_at=NOW + timedelta(minutes=3),
        )
    )
    repeated = asyncio.run(
        storage.transition(
            pending,
            status=MediaRetentionWorkStatus.COMPLETED,
            attempt_count=2,
            next_attempt_at=None,
            last_error_code=None,
            updated_at=NOW + timedelta(minutes=3),
        )
    )

    assert completed.record.status is MediaRetentionWorkStatus.COMPLETED
    assert repeated == completed


def test_transition_retry_reads_back_after_lost_success_response():
    storage, client = storage_for()
    operation = build_work_item()
    asyncio.run(client.execute_batch(partition_key="home", operations=(operation,)))
    initial = asyncio.run(storage.get(household_id="home", operation_id="operation-1"))
    assert initial is not None
    client.timeout_after_commit = True
    transition_args = {
        "status": MediaRetentionWorkStatus.PENDING,
        "attempt_count": 1,
        "next_attempt_at": NOW + timedelta(minutes=2),
        "last_error_code": "http_503",
        "updated_at": NOW + timedelta(seconds=1),
    }

    with pytest.raises(TimeoutError, match="lost response"):
        asyncio.run(storage.transition(initial, **transition_args))

    recovered = asyncio.run(storage.transition(initial, **transition_args))

    assert recovered.record.attempt_count == 1
    assert recovered.record.last_error_code == "http_503"


def test_permanent_failure_moves_to_needs_attention():
    storage, client = storage_for()
    operation = build_work_item()
    asyncio.run(client.execute_batch(partition_key="home", operations=(operation,)))
    initial = asyncio.run(storage.get(household_id="home", operation_id="operation-1"))
    assert initial is not None

    updated = asyncio.run(
        storage.transition(
            initial,
            status=MediaRetentionWorkStatus.NEEDS_ATTENTION,
            attempt_count=1,
            next_attempt_at=None,
            last_error_code="blob_not_found",
            updated_at=NOW + timedelta(seconds=1),
        )
    )

    assert updated.record.status is MediaRetentionWorkStatus.NEEDS_ATTENTION
    assert updated.record.next_attempt_at is None
    assert updated.record.last_error_code == "blob_not_found"


def test_transition_rejects_stale_etag_and_terminal_reopen():
    storage, client = storage_for()
    operation = build_work_item()
    asyncio.run(client.execute_batch(partition_key="home", operations=(operation,)))
    initial = asyncio.run(storage.get(household_id="home", operation_id="operation-1"))
    assert initial is not None

    rescheduled = asyncio.run(
        storage.transition(
            initial,
            status=MediaRetentionWorkStatus.PENDING,
            attempt_count=1,
            next_attempt_at=NOW + timedelta(minutes=1),
            last_error_code="http_503",
            updated_at=NOW + timedelta(seconds=1),
        )
    )
    with pytest.raises(BatchOperationConflict):
        asyncio.run(
            storage.transition(
                initial,
                status=MediaRetentionWorkStatus.COMPLETED,
                attempt_count=1,
                next_attempt_at=None,
                last_error_code=None,
                updated_at=NOW + timedelta(seconds=2),
            )
        )

    completed = asyncio.run(
        storage.transition(
            rescheduled,
            status=MediaRetentionWorkStatus.COMPLETED,
            attempt_count=2,
            next_attempt_at=None,
            last_error_code=None,
            updated_at=NOW + timedelta(minutes=2),
        )
    )
    with pytest.raises(MediaRetentionWorkItemConflict, match="completed"):
        asyncio.run(
            storage.transition(
                completed,
                status=MediaRetentionWorkStatus.PENDING,
                attempt_count=3,
                next_attempt_at=NOW + timedelta(minutes=3),
                last_error_code="http_503",
                updated_at=NOW + timedelta(minutes=3),
            )
        )


def test_transition_rejects_invalid_error_code_and_state_metadata():
    storage, client = storage_for()
    operation = build_work_item()
    asyncio.run(client.execute_batch(partition_key="home", operations=(operation,)))
    initial = asyncio.run(storage.get(household_id="home", operation_id="operation-1"))
    assert initial is not None

    with pytest.raises(ValueError, match="short lowercase code"):
        asyncio.run(
            storage.transition(
                initial,
                status=MediaRetentionWorkStatus.NEEDS_ATTENTION,
                attempt_count=1,
                next_attempt_at=None,
                last_error_code="HTTP 404: blob not found",
                updated_at=NOW + timedelta(seconds=1),
            )
        )

    with pytest.raises(ValueError, match="requires next_attempt_at"):
        asyncio.run(
            storage.transition(
                initial,
                status=MediaRetentionWorkStatus.PENDING,
                attempt_count=1,
                next_attempt_at=None,
                last_error_code=None,
                updated_at=NOW + timedelta(seconds=1),
            )
        )

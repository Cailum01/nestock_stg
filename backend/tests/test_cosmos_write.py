import asyncio
from datetime import UTC, datetime

import pytest

from app.models.domain import ChangeEvent
from app.services.cosmos_read import CosmosDocumentMapper
from app.services.cosmos_write import (
    ApprovalIdConflict,
    BatchOperationConflict,
    CosmosWriteRepository,
    CreateDocument,
    OperationIdConflict,
    OperationReceipt,
    OperationReceiptInconsistency,
    PendingApprovalRecord,
    ReplaceDocument,
    WriteStatus,
    change_event_create,
)


class FakeTransactionalBatchClient:
    def __init__(self):
        self.documents = {}
        self.calls = []
        self.read_calls = []
        self.fail_operation_index = None
        self.timeout_after_commit = False

    async def execute_batch(self, *, partition_key, operations):
        self.calls.append({"partition_key": partition_key, "operations": tuple(operations)})
        if self.fail_operation_index is not None:
            raise BatchOperationConflict(self.fail_operation_index)

        staged = {}
        for index, operation in enumerate(operations):
            document = dict(operation.document)
            if document["household_id"] != partition_key:
                raise ValueError("Invalid fake batch partition.")
            if isinstance(operation, CreateDocument):
                key = (partition_key, document["id"])
                if key in self.documents or key in staged:
                    raise BatchOperationConflict(index)
                stored_document = dict(document)
                if stored_document.get("record_type") == "pending_approval":
                    stored_document["_etag"] = '"etag-1"'
                staged[key] = stored_document
            elif isinstance(operation, ReplaceDocument):
                key = (partition_key, document["id"])
                current = self.documents.get(key)
                if current is None or current.get("_etag") != operation.if_match_etag:
                    raise BatchOperationConflict(index)
                updated_document = dict(document)
                if updated_document.get("record_type") == "pending_approval":
                    updated_document["_etag"] = '"etag-2"'
                staged[key] = updated_document

        self.documents.update(staged)
        if self.timeout_after_commit:
            self.timeout_after_commit = False
            raise TimeoutError("Simulated lost response after committed batch.")

    async def read_item(self, *, item_id, partition_key):
        self.read_calls.append({"item_id": item_id, "partition_key": partition_key})
        return self.documents.get((partition_key, item_id))


class FakeApprovalQueryClient:
    def __init__(self, documents):
        self.documents = documents
        self.calls = []

    async def query_items(self, *, query, parameters, partition_key):
        self.calls.append(
            {
                "query": query,
                "parameters": parameters,
                "partition_key": partition_key,
            }
        )
        return self.documents


def inventory_document(*, quantity=2, household_id="home"):
    return {
        "id": "inventory_item:rice",
        "record_type": "inventory_item",
        "household_id": household_id,
        "item_id": "rice",
        "quantity": quantity,
    }


def pending_approval_document(*, household_id="home", approval_id="approval-1"):
    return {
        "id": f"pending_approval:{approval_id}",
        "record_type": "pending_approval",
        "household_id": household_id,
        "approval_id": approval_id,
        "status": "pending",
        "created_at": "2026-11-06T00:00:00Z",
        "expires_at": "2026-11-06T00:30:00Z",
        "operations": [],
    }


def test_create_pending_approval_stores_ttl_and_returns_etag():
    client = FakeTransactionalBatchClient()
    repository = CosmosWriteRepository(client)

    approval = asyncio.run(
        repository.create_pending_approval(
            household_id="home",
            approval=pending_approval_document(),
            ttl_seconds=3600,
        )
    )

    assert approval.approval_id == "approval-1"
    assert approval.etag == '"etag-1"'
    assert approval.document["ttl"] == 3600
    assert client.calls[0]["partition_key"] == "home"
    assert len(client.calls[0]["operations"]) == 1
    assert client.calls[0]["operations"][0].document["record_type"] == "pending_approval"


def test_create_pending_approval_retry_is_idempotent_for_same_document():
    client = FakeTransactionalBatchClient()
    repository = CosmosWriteRepository(client)
    document = pending_approval_document()

    first = asyncio.run(
        repository.create_pending_approval(
            household_id="home",
            approval=document,
            ttl_seconds=3600,
        )
    )
    retry = asyncio.run(
        repository.create_pending_approval(
            household_id="home",
            approval=document,
            ttl_seconds=3600,
        )
    )

    assert retry == first
    assert len(client.documents) == 1
    assert len(client.calls) == 2


def test_create_pending_approval_retry_after_lost_response_returns_existing_record():
    client = FakeTransactionalBatchClient()
    client.timeout_after_commit = True
    repository = CosmosWriteRepository(client)
    document = pending_approval_document()

    with pytest.raises(TimeoutError, match="lost response"):
        asyncio.run(
            repository.create_pending_approval(
                household_id="home",
                approval=document,
                ttl_seconds=3600,
            )
        )

    retry = asyncio.run(
        repository.create_pending_approval(
            household_id="home",
            approval=document,
            ttl_seconds=3600,
        )
    )

    assert retry.etag == '"etag-1"'
    assert len(client.documents) == 1


def test_create_pending_approval_rejects_reused_id_with_different_values():
    client = FakeTransactionalBatchClient()
    repository = CosmosWriteRepository(client)
    asyncio.run(
        repository.create_pending_approval(
            household_id="home",
            approval=pending_approval_document(),
            ttl_seconds=3600,
        )
    )

    with pytest.raises(ApprovalIdConflict, match="different canonical values"):
        asyncio.run(
            repository.create_pending_approval(
                household_id="home",
                approval=pending_approval_document() | {"operations": [{"operation_id": "op-2"}]},
                ttl_seconds=3600,
            )
        )


@pytest.mark.parametrize(
    ("household_id", "approval", "ttl_seconds"),
    [
        ("", pending_approval_document(), 3600),
        ("home", pending_approval_document() | {"status": "confirmed"}, 3600),
        ("home", pending_approval_document() | {"id": "wrong-id"}, 3600),
        ("home", pending_approval_document() | {"household_id": "other"}, 3600),
        ("home", pending_approval_document(), 0),
        ("home", pending_approval_document(), True),
        ("home", pending_approval_document(), "3600"),
    ],
)
def test_create_pending_approval_rejects_invalid_record_or_ttl(
    household_id,
    approval,
    ttl_seconds,
):
    repository = CosmosWriteRepository(FakeTransactionalBatchClient())

    with pytest.raises(ValueError):
        asyncio.run(
            repository.create_pending_approval(
                household_id=household_id,
                approval=approval,
                ttl_seconds=ttl_seconds,
            )
        )


def test_approval_only_etag_replace_returns_updated_record_and_etag():
    client = FakeTransactionalBatchClient()
    document = pending_approval_document()
    document["ttl"] = 3600
    document["_etag"] = '"etag-1"'
    client.documents[("home", document["id"])] = document
    repository = CosmosWriteRepository(client)
    current = asyncio.run(
        repository.read_pending_approval(household_id="home", approval_id="approval-1")
    )
    assert current is not None

    updated = asyncio.run(
        repository.replace_pending_approval(
            household_id="home",
            approval_id="approval-1",
            document=current.document
            | {
                "status": "confirmed",
                "selection": {"approved_operation_ids": ["op-1"]},
                "decided_at": "2026-11-06T00:05:00Z",
            },
            if_match_etag=current.etag,
        )
    )

    assert updated.document["status"] == "confirmed"
    assert updated.etag == '"etag-2"'
    assert len(client.calls[-1]["operations"]) == 1
    assert isinstance(client.calls[-1]["operations"][0], ReplaceDocument)
    assert "_etag" not in client.calls[-1]["operations"][0].document
    assert client.calls[-1]["operations"][0].if_match_etag == '"etag-1"'


def test_approval_only_etag_replace_after_lost_response_is_readable():
    client = FakeTransactionalBatchClient()
    document = pending_approval_document() | {"_etag": '"etag-1"'}
    client.documents[("home", document["id"])] = document
    client.timeout_after_commit = True
    repository = CosmosWriteRepository(client)

    with pytest.raises(TimeoutError, match="lost response"):
        asyncio.run(
            repository.replace_pending_approval(
                household_id="home",
                approval_id="approval-1",
                document=document | {"status": "confirmed"},
                if_match_etag='"etag-1"',
            )
        )

    current = asyncio.run(
        repository.read_pending_approval(household_id="home", approval_id="approval-1")
    )
    assert current is not None
    assert current.document["status"] == "confirmed"
    assert current.etag == '"etag-2"'


@pytest.mark.parametrize("status", ["cancelled", "expired"])
def test_approval_only_etag_replace_supports_terminal_transitions(status):
    client = FakeTransactionalBatchClient()
    document = pending_approval_document() | {"_etag": '"etag-1"'}
    client.documents[("home", document["id"])] = document
    repository = CosmosWriteRepository(client)

    updated = asyncio.run(
        repository.replace_pending_approval(
            household_id="home",
            approval_id="approval-1",
            document=document | {"status": status},
            if_match_etag='"etag-1"',
        )
    )

    assert updated.document["status"] == status


def test_approval_only_etag_replace_rejects_completed_and_stale_etag():
    client = FakeTransactionalBatchClient()
    document = pending_approval_document() | {"_etag": '"etag-current"'}
    client.documents[("home", document["id"])] = document
    repository = CosmosWriteRepository(client)

    with pytest.raises(ValueError, match="status must be confirmed"):
        asyncio.run(
            repository.replace_pending_approval(
                household_id="home",
                approval_id="approval-1",
                document=document | {"status": "completed"},
                if_match_etag='"etag-current"',
            )
        )

    with pytest.raises(BatchOperationConflict):
        asyncio.run(
            repository.replace_pending_approval(
                household_id="home",
                approval_id="approval-1",
                document=document | {"status": "cancelled"},
                if_match_etag='"etag-stale"',
            )
        )

    assert client.documents[("home", document["id"])] == document


@pytest.mark.parametrize(
    ("current_status", "new_status"),
    [("cancelled", "confirmed"), ("completed", "cancelled"), ("confirmed", "confirmed")],
)
def test_approval_only_etag_replace_rejects_invalid_transitions(current_status, new_status):
    client = FakeTransactionalBatchClient()
    document = pending_approval_document() | {
        "status": current_status,
        "_etag": '"etag-current"',
    }
    client.documents[("home", document["id"])] = document
    repository = CosmosWriteRepository(client)

    with pytest.raises(ValueError, match="cannot transition"):
        asyncio.run(
            repository.replace_pending_approval(
                household_id="home",
                approval_id="approval-1",
                document=document | {"status": new_status},
                if_match_etag='"etag-current"',
            )
        )

    assert client.documents[("home", document["id"])] == document
    assert client.calls == []


def test_operation_id_retry_returns_already_applied_without_rewriting():
    client = FakeTransactionalBatchClient()
    repository = CosmosWriteRepository(client)
    operations = [CreateDocument(inventory_document())]

    first = asyncio.run(
        repository.execute_idempotent_batch(
            household_id="home",
            operation_id="op-1",
            operations=operations,
        )
    )
    retry = asyncio.run(
        repository.execute_idempotent_batch(
            household_id="home",
            operation_id="op-1",
            operations=operations,
        )
    )

    assert first == WriteStatus.APPLIED
    assert retry == WriteStatus.ALREADY_APPLIED
    assert len(client.calls) == 2
    assert client.calls[0]["partition_key"] == "home"
    receipt = next(
        document
        for document in client.documents.values()
        if document["record_type"] == "operation_receipt"
    )
    assert receipt["record_type"] == "operation_receipt"
    assert receipt["operation_id"] == "op-1"
    assert client.documents[("home", "inventory_item:rice")] == inventory_document()


def test_pending_approval_read_returns_household_scoped_record_and_etag():
    client = FakeTransactionalBatchClient()
    document = {
        "id": "pending_approval:approval-1",
        "record_type": "pending_approval",
        "household_id": "home",
        "approval_id": "approval-1",
        "_etag": '"etag-1"',
    }
    client.documents[("home", document["id"])] = document
    repository = CosmosWriteRepository(client)

    approval = asyncio.run(
        repository.read_pending_approval(household_id="home", approval_id="approval-1")
    )

    assert isinstance(approval, PendingApprovalRecord)
    assert approval.household_id == "home"
    assert approval.approval_id == "approval-1"
    assert approval.etag == '"etag-1"'
    assert approval.document == document
    assert client.read_calls[-1] == {
        "item_id": "pending_approval:approval-1",
        "partition_key": "home",
    }


def test_pending_approval_read_returns_none_when_not_found_in_household():
    client = FakeTransactionalBatchClient()
    repository = CosmosWriteRepository(client)

    approval = asyncio.run(
        repository.read_pending_approval(household_id="home", approval_id="approval-1")
    )

    assert approval is None
    assert client.read_calls[-1]["partition_key"] == "home"


def test_pending_approval_read_rejects_mismatched_document_identity():
    client = FakeTransactionalBatchClient()
    document = {
        "id": "pending_approval:approval-1",
        "record_type": "pending_approval",
        "household_id": "home",
        "approval_id": "approval-other",
        "_etag": '"etag-1"',
    }
    client.documents[("home", document["id"])] = document
    repository = CosmosWriteRepository(client)

    with pytest.raises(ValueError, match="does not match its partition key"):
        asyncio.run(repository.read_pending_approval(household_id="home", approval_id="approval-1"))


def test_pending_approval_read_requires_etag_for_conditional_batch_updates():
    client = FakeTransactionalBatchClient()
    document = {
        "id": "pending_approval:approval-1",
        "record_type": "pending_approval",
        "household_id": "home",
        "approval_id": "approval-1",
    }
    client.documents[("home", document["id"])] = document
    repository = CosmosWriteRepository(client)

    with pytest.raises(ValueError, match="no valid Cosmos _etag"):
        asyncio.run(repository.read_pending_approval(household_id="home", approval_id="approval-1"))


def test_list_open_approvals_queries_one_household_and_returns_pending_and_confirmed():
    documents = [
        {
            "id": f"pending_approval:{status}-1",
            "record_type": "pending_approval",
            "household_id": "home",
            "approval_id": f"{status}-1",
            "status": status,
            "_etag": f'"etag-{status}"',
        }
        for status in ("pending", "confirmed")
    ]
    query_client = FakeApprovalQueryClient(documents)
    repository = CosmosWriteRepository(FakeTransactionalBatchClient(), query_client=query_client)

    approvals = asyncio.run(repository.list_open_approvals(household_id="home"))

    assert [approval.approval_id for approval in approvals] == ["pending-1", "confirmed-1"]
    assert [approval.etag for approval in approvals] == ['"etag-pending"', '"etag-confirmed"']
    assert query_client.calls == [
        {
            "query": (
                "SELECT * FROM c WHERE c.record_type = @record_type "
                "AND c.household_id = @household_id "
                "AND c.status IN (@pending_status, @confirmed_status)"
            ),
            "parameters": (
                {"name": "@record_type", "value": "pending_approval"},
                {"name": "@household_id", "value": "home"},
                {"name": "@pending_status", "value": "pending"},
                {"name": "@confirmed_status", "value": "confirmed"},
            ),
            "partition_key": "home",
        }
    ]


def test_list_open_approvals_requires_a_household_scoped_query_client():
    repository = CosmosWriteRepository(FakeTransactionalBatchClient())

    with pytest.raises(RuntimeError, match="requires a household-scoped query client"):
        asyncio.run(repository.list_open_approvals(household_id="home"))


@pytest.mark.parametrize(
    ("household_id", "document", "message"),
    [
        ("", {}, "household_id must not be empty"),
        (
            "home",
            {
                "id": "pending_approval:approval-1",
                "record_type": "pending_approval",
                "household_id": "other",
                "approval_id": "approval-1",
                "status": "pending",
                "_etag": '"etag-1"',
            },
            "does not match its partition key",
        ),
        (
            "home",
            {
                "id": "pending_approval:approval-1",
                "record_type": "pending_approval",
                "household_id": "home",
                "approval_id": "approval-1",
                "status": "completed",
                "_etag": '"etag-1"',
            },
            "non-open record",
        ),
        (
            "home",
            {
                "id": "pending_approval:approval-1",
                "record_type": "pending_approval",
                "household_id": "home",
                "approval_id": "approval-1",
                "status": "pending",
            },
            "no valid Cosmos _etag",
        ),
    ],
)
def test_list_open_approvals_rejects_invalid_scope_or_records(household_id, document, message):
    query_client = FakeApprovalQueryClient([document])
    repository = CosmosWriteRepository(FakeTransactionalBatchClient(), query_client=query_client)

    with pytest.raises(ValueError, match=message):
        asyncio.run(repository.list_open_approvals(household_id=household_id))


def test_retry_after_lost_success_response_is_reported_as_already_applied():
    client = FakeTransactionalBatchClient()
    client.timeout_after_commit = True
    repository = CosmosWriteRepository(client)
    operations = [CreateDocument(inventory_document())]

    with pytest.raises(TimeoutError, match="lost response"):
        asyncio.run(
            repository.execute_idempotent_batch(
                household_id="home",
                operation_id="op-1",
                operations=operations,
            )
        )

    retry = asyncio.run(
        repository.execute_idempotent_batch(
            household_id="home",
            operation_id="op-1",
            operations=operations,
        )
    )

    assert retry == WriteStatus.ALREADY_APPLIED
    assert len(client.calls) == 2
    assert len(client.documents) == 2


def test_operation_receipt_can_be_read_by_household_and_operation_identity():
    client = FakeTransactionalBatchClient()
    repository = CosmosWriteRepository(client)
    asyncio.run(
        repository.execute_idempotent_batch(
            household_id="home",
            operation_id="op-1",
            operations=[CreateDocument(inventory_document())],
        )
    )

    receipt = asyncio.run(
        repository.read_operation_receipt(household_id="home", operation_id="op-1")
    )

    assert isinstance(receipt, OperationReceipt)
    assert receipt.household_id == "home"
    assert receipt.operation_id == "op-1"
    assert receipt.request_fingerprint
    assert receipt.batch_fingerprint
    assert client.read_calls[-1]["partition_key"] == "home"


def test_operation_receipt_lookup_is_household_scoped_and_returns_none_when_missing():
    client = FakeTransactionalBatchClient()
    repository = CosmosWriteRepository(client)
    asyncio.run(
        repository.execute_idempotent_batch(
            household_id="home-a",
            operation_id="op-1",
            operations=[CreateDocument(inventory_document(household_id="home-a"))],
        )
    )

    receipt = asyncio.run(
        repository.read_operation_receipt(household_id="home-b", operation_id="op-1")
    )

    assert receipt is None
    assert client.read_calls[-1]["partition_key"] == "home-b"


def test_operation_receipt_lookup_rejects_malformed_receipt():
    client = FakeTransactionalBatchClient()
    repository = CosmosWriteRepository(client)
    asyncio.run(
        repository.execute_idempotent_batch(
            household_id="home",
            operation_id="op-1",
            operations=[CreateDocument(inventory_document())],
        )
    )
    receipt_key = next(
        key
        for key, document in client.documents.items()
        if document["record_type"] == "operation_receipt"
    )
    client.documents[receipt_key]["record_type"] = "inventory_item"

    with pytest.raises(ValueError, match="does not match its partition key"):
        asyncio.run(repository.read_operation_receipt(household_id="home", operation_id="op-1"))


def test_operation_receipt_lookup_rejects_missing_fingerprint():
    client = FakeTransactionalBatchClient()
    repository = CosmosWriteRepository(client)
    asyncio.run(
        repository.execute_idempotent_batch(
            household_id="home",
            operation_id="op-1",
            operations=[CreateDocument(inventory_document())],
        )
    )
    receipt_key = next(
        key
        for key, document in client.documents.items()
        if document["record_type"] == "operation_receipt"
    )
    del client.documents[receipt_key]["request_fingerprint"]

    with pytest.raises(ValueError, match="no valid request fingerprint"):
        asyncio.run(repository.read_operation_receipt(household_id="home", operation_id="op-1"))


def test_approval_etag_replace_can_join_multi_operation_household_batch():
    client = FakeTransactionalBatchClient()
    approval_document = {
        "id": "pending_approval:approval-1",
        "record_type": "pending_approval",
        "household_id": "home",
        "approval_id": "approval-1",
        "marker": "before",
        "_etag": '"etag-1"',
    }
    client.documents[("home", approval_document["id"])] = approval_document
    repository = CosmosWriteRepository(client)
    approval = asyncio.run(
        repository.read_pending_approval(household_id="home", approval_id="approval-1")
    )
    assert approval is not None
    updated_approval = dict(approval.document) | {"marker": "after"}

    status = asyncio.run(
        repository.execute_idempotent_batches(
            household_id="home",
            operations_by_id={
                "op-1": [CreateDocument({"id": "inventory:1", "household_id": "home"})],
                "op-2": [CreateDocument({"id": "transaction:1", "household_id": "home"})],
            },
            shared_operations=[
                ReplaceDocument(
                    document=updated_approval,
                    if_match_etag=approval.etag,
                )
            ],
        )
    )

    assert status == WriteStatus.APPLIED
    assert client.documents[("home", approval_document["id"])]["marker"] == "after"
    assert ("home", "inventory:1") in client.documents
    assert ("home", "transaction:1") in client.documents
    assert len(client.calls[-1]["operations"]) == 5


def test_approval_etag_conflict_aborts_entire_multi_operation_batch():
    client = FakeTransactionalBatchClient()
    approval_document = {
        "id": "pending_approval:approval-1",
        "record_type": "pending_approval",
        "household_id": "home",
        "approval_id": "approval-1",
        "_etag": '"etag-current"',
    }
    client.documents[("home", approval_document["id"])] = approval_document
    repository = CosmosWriteRepository(client)

    with pytest.raises(BatchOperationConflict):
        asyncio.run(
            repository.execute_idempotent_batches(
                household_id="home",
                operations_by_id={
                    "op-1": [CreateDocument({"id": "inventory:1", "household_id": "home"})]
                },
                shared_operations=[
                    ReplaceDocument(
                        document=dict(approval_document) | {"marker": "after"},
                        if_match_etag='"etag-stale"',
                    )
                ],
            )
        )

    assert ("home", "inventory:1") not in client.documents
    assert client.documents[("home", approval_document["id"])] == approval_document


def test_multiple_operation_ids_create_receipts_in_one_atomic_batch():
    client = FakeTransactionalBatchClient()
    repository = CosmosWriteRepository(client)

    status = asyncio.run(
        repository.execute_idempotent_batches(
            household_id="home",
            operations_by_id={
                "op-2": [CreateDocument({"id": "shopping:2", "household_id": "home"})],
                "op-1": [CreateDocument({"id": "inventory:1", "household_id": "home"})],
            },
        )
    )

    assert status == WriteStatus.APPLIED
    assert len(client.calls) == 1
    receipts = [
        operation.document
        for operation in client.calls[0]["operations"]
        if operation.document.get("record_type") == "operation_receipt"
    ]
    assert [receipt["operation_id"] for receipt in receipts] == ["op-1", "op-2"]
    assert len(client.documents) == 4


def test_multiple_operation_ids_retry_as_already_applied():
    client = FakeTransactionalBatchClient()
    repository = CosmosWriteRepository(client)
    operations_by_id = {
        "op-1": [CreateDocument({"id": "inventory:1", "household_id": "home"})],
        "op-2": [CreateDocument({"id": "shopping:2", "household_id": "home"})],
    }

    first = asyncio.run(
        repository.execute_idempotent_batches(
            household_id="home",
            operations_by_id=operations_by_id,
        )
    )
    retry = asyncio.run(
        repository.execute_idempotent_batches(
            household_id="home",
            operations_by_id=operations_by_id,
        )
    )

    assert first == WriteStatus.APPLIED
    assert retry == WriteStatus.ALREADY_APPLIED
    assert len(client.documents) == 4


def test_multiple_operation_batch_requires_reconciliation_for_partial_receipts():
    client = FakeTransactionalBatchClient()
    repository = CosmosWriteRepository(client)
    first_operation = [CreateDocument({"id": "inventory:1", "household_id": "home"})]
    asyncio.run(
        repository.execute_idempotent_batch(
            household_id="home",
            operation_id="op-1",
            operations=first_operation,
        )
    )

    with pytest.raises(OperationReceiptInconsistency, match="reconciliation is required"):
        asyncio.run(
            repository.execute_idempotent_batches(
                household_id="home",
                operations_by_id={
                    "op-1": first_operation,
                    "op-2": [CreateDocument({"id": "shopping:2", "household_id": "home"})],
                },
            )
        )

    assert ("home", "shopping:2") not in client.documents
    assert len(client.documents) == 2


def test_multiple_operation_batch_rejects_reused_id_with_changed_payload():
    client = FakeTransactionalBatchClient()
    repository = CosmosWriteRepository(client)
    asyncio.run(
        repository.execute_idempotent_batches(
            household_id="home",
            operations_by_id={
                "op-1": [CreateDocument({"id": "inventory:1", "household_id": "home"})],
                "op-2": [CreateDocument({"id": "shopping:2", "household_id": "home"})],
            },
        )
    )

    with pytest.raises(OperationIdConflict, match="op-1.*different canonical values"):
        asyncio.run(
            repository.execute_idempotent_batches(
                household_id="home",
                operations_by_id={
                    "op-1": [
                        CreateDocument(
                            {
                                "id": "inventory:1",
                                "household_id": "home",
                                "quantity": 3,
                            }
                        )
                    ],
                    "op-2": [CreateDocument({"id": "shopping:2", "household_id": "home"})],
                },
            )
        )


@pytest.mark.parametrize(("household_id", "operation_id"), [("", "op-1"), ("home", "")])
def test_operation_receipt_lookup_validates_scope_and_identity(household_id, operation_id):
    client = FakeTransactionalBatchClient()
    repository = CosmosWriteRepository(client)

    with pytest.raises(ValueError):
        asyncio.run(
            repository.read_operation_receipt(
                household_id=household_id,
                operation_id=operation_id,
            )
        )

    assert not client.read_calls


def test_reusing_operation_id_with_different_values_is_a_conflict():
    client = FakeTransactionalBatchClient()
    repository = CosmosWriteRepository(client)
    asyncio.run(
        repository.execute_idempotent_batch(
            household_id="home",
            operation_id="op-1",
            operations=[CreateDocument(inventory_document(quantity=2))],
        )
    )

    with pytest.raises(OperationIdConflict, match="different canonical values"):
        asyncio.run(
            repository.execute_idempotent_batch(
                household_id="home",
                operation_id="op-1",
                operations=[CreateDocument(inventory_document(quantity=3))],
            )
        )


def test_multiple_operation_batch_rejects_changed_batch_membership():
    client = FakeTransactionalBatchClient()
    repository = CosmosWriteRepository(client)
    asyncio.run(
        repository.execute_idempotent_batches(
            household_id="home",
            operations_by_id={
                "op-1": [CreateDocument({"id": "inventory:1", "household_id": "home"})],
                "op-2": [CreateDocument({"id": "shopping:2", "household_id": "home"})],
            },
        )
    )

    with pytest.raises(OperationIdConflict, match="batch membership"):
        asyncio.run(
            repository.execute_idempotent_batches(
                household_id="home",
                operations_by_id={
                    "op-1": [CreateDocument({"id": "inventory:1", "household_id": "home"})]
                },
            )
        )


def test_operation_id_is_scoped_to_household_partition():
    client = FakeTransactionalBatchClient()
    repository = CosmosWriteRepository(client)

    first = asyncio.run(
        repository.execute_idempotent_batch(
            household_id="home-a",
            operation_id="same-op",
            operations=[CreateDocument(inventory_document(household_id="home-a"))],
        )
    )
    second = asyncio.run(
        repository.execute_idempotent_batch(
            household_id="home-b",
            operation_id="same-op",
            operations=[CreateDocument(inventory_document(household_id="home-b"))],
        )
    )

    assert first == WriteStatus.APPLIED
    assert second == WriteStatus.APPLIED
    receipts = [
        document
        for document in client.documents.values()
        if document["record_type"] == "operation_receipt"
    ]
    assert len(receipts) == 2
    assert {receipt["household_id"] for receipt in receipts} == {"home-a", "home-b"}


def test_non_receipt_batch_conflict_is_not_misreported_as_an_idempotent_retry():
    client = FakeTransactionalBatchClient()
    client.fail_operation_index = 1
    repository = CosmosWriteRepository(client)

    with pytest.raises(BatchOperationConflict) as error:
        asyncio.run(
            repository.execute_idempotent_batch(
                household_id="home",
                operation_id="op-1",
                operations=[CreateDocument(inventory_document())],
            )
        )

    assert error.value.operation_index == 1
    assert client.documents == {}


@pytest.mark.parametrize(
    ("household_id", "operation_id", "operations"),
    [
        ("", "op-1", [CreateDocument(inventory_document())]),
        ("home", "", [CreateDocument(inventory_document())]),
        ("home", "op-1", []),
        ("home", "op-1", [CreateDocument(inventory_document(household_id="other"))]),
        (
            "home",
            "op-1",
            [
                CreateDocument(
                    {
                        "id": "operation:caller",
                        "record_type": "operation_receipt",
                        "household_id": "home",
                    }
                )
            ],
        ),
    ],
)
def test_batch_validates_idempotency_scope_and_receipt_ownership(
    household_id, operation_id, operations
):
    repository = CosmosWriteRepository(FakeTransactionalBatchClient())

    with pytest.raises(ValueError):
        asyncio.run(
            repository.execute_idempotent_batch(
                household_id=household_id,
                operation_id=operation_id,
                operations=operations,
            )
        )


def test_replace_requires_etag_but_fingerprints_only_canonical_document_values():
    client = FakeTransactionalBatchClient()
    client.documents[("home", "inventory_item:rice")] = inventory_document() | {"_etag": '"etag-1"'}
    repository = CosmosWriteRepository(client)
    operation = ReplaceDocument(
        document=inventory_document(quantity=3),
        if_match_etag='"etag-1"',
    )

    status = asyncio.run(
        repository.execute_idempotent_batch(
            household_id="home",
            operation_id="op-1",
            operations=[operation],
        )
    )

    assert status == WriteStatus.APPLIED
    assert client.documents[("home", "inventory_item:rice")]["quantity"] == 3
    assert client.calls[0]["operations"][1].if_match_etag == '"etag-1"'

    retry = asyncio.run(
        repository.execute_idempotent_batch(
            household_id="home",
            operation_id="op-1",
            operations=[
                ReplaceDocument(
                    document=inventory_document(quantity=3),
                    if_match_etag='"etag-after-write"',
                )
            ],
        )
    )

    assert retry == WriteStatus.ALREADY_APPLIED


def _change_event(operation_id="op-1"):
    return ChangeEvent(
        event_id=operation_id,
        household_id="home",
        operation_id=operation_id,
        approval_id="approval-1",
        transaction_id=f"txn-{operation_id}",
        shopping_item_id=f"shop-{operation_id}",
        item_id="rice",
        occurred_at=datetime(2026, 11, 7, 4, 0, tzinfo=UTC),
        recorded_at=datetime(2026, 11, 8, 4, 0, tzinfo=UTC),
    )


def test_change_event_is_created_in_the_operation_batch_and_retry_is_idempotent():
    client = FakeTransactionalBatchClient()
    repository = CosmosWriteRepository(client)
    event = _change_event()

    def run():
        return asyncio.run(
            repository.execute_idempotent_batch(
                household_id="home",
                operation_id="op-1",
                operations=[
                    CreateDocument({"id": "transaction:txn-op-1", "household_id": "home"}),
                    change_event_create(event),
                ],
            )
        )

    assert run() == WriteStatus.APPLIED
    stored = client.documents[("home", "change_event:op-1")]
    assert stored["record_type"] == "change_event"
    assert stored["occurred_at"] == "2026-11-07T04:00:00.000000Z"
    assert CosmosDocumentMapper.from_document(ChangeEvent, stored, household_id="home") == event

    assert run() == WriteStatus.ALREADY_APPLIED
    assert len([key for key in client.documents if key[1] == "change_event:op-1"]) == 1


def test_change_event_without_shopping_item_roundtrips_through_cosmos_mapping():
    event = ChangeEvent(
        event_id="photo-operation",
        household_id="home",
        operation_id="photo-operation",
        approval_id="approval-photo",
        transaction_id="txn-photo",
        item_id="new-item",
        occurred_at=datetime(2026, 11, 16, 4, 0, tzinfo=UTC),
        recorded_at=datetime(2026, 11, 16, 4, 0, tzinfo=UTC),
    )

    document = CosmosDocumentMapper.to_document(event)

    assert document["shopping_item_id"] is None
    assert CosmosDocumentMapper.from_document(ChangeEvent, document, household_id="home") == event


def test_change_event_create_requires_event_id_to_equal_operation_id():
    event = _change_event().model_copy(update={"event_id": "other"})

    with pytest.raises(ValueError):
        change_event_create(event)

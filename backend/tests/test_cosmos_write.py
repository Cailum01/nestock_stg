import asyncio

import pytest

from app.services.cosmos_write import (
    BatchOperationConflict,
    CosmosWriteRepository,
    CreateDocument,
    OperationIdConflict,
    OperationReceipt,
    OperationReceiptInconsistency,
    PendingApprovalRecord,
    ReplaceDocument,
    WriteStatus,
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
                staged[key] = document
            elif isinstance(operation, ReplaceDocument):
                key = (partition_key, document["id"])
                current = self.documents.get(key)
                if current is None or current.get("_etag") != operation.if_match_etag:
                    raise BatchOperationConflict(index)
                staged[key] = document

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

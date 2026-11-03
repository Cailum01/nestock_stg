import asyncio

import pytest

from app.services.cosmos_write import (
    BatchOperationConflict,
    CosmosWriteRepository,
    CreateDocument,
    OperationIdConflict,
    ReplaceDocument,
    WriteStatus,
)


class FakeTransactionalBatchClient:
    def __init__(self):
        self.documents = {}
        self.calls = []
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
        return self.documents.get((partition_key, item_id))


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

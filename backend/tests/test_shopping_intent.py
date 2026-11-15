import asyncio
from datetime import UTC, datetime

import pytest

from app.services.cosmos_write import (
    BatchOperationConflict,
    CosmosWriteRepository,
    CreateDocument,
    OperationIdConflict,
    ReplaceDocument,
    WriteStatus,
)
from app.services.shopping.identity import resolve_shopping_intent_identity
from app.services.shopping.intents import (
    AddShoppingIntent,
    ShoppingIntentError,
    ShoppingIntentService,
)


class FakeTransactionalBatchClient:
    def __init__(self):
        self.documents = {}
        self.batch_calls = []

    async def execute_batch(self, *, partition_key, operations):
        self.batch_calls.append((partition_key, tuple(operations)))
        staged = {}
        for index, operation in enumerate(operations):
            document = dict(operation.document)
            if document["household_id"] != partition_key:
                raise ValueError("Invalid fake batch partition.")
            key = (partition_key, document["id"])
            if isinstance(operation, CreateDocument):
                if key in self.documents or key in staged:
                    raise BatchOperationConflict(index)
                staged[key] = document
            elif isinstance(operation, ReplaceDocument):
                raise AssertionError("Shopping Intent add must only create documents.")
        self.documents.update(staged)

    async def read_item(self, *, item_id, partition_key):
        return self.documents.get((partition_key, item_id))


def make_command(**changes):
    values = {
        "household_id": "demo-household",
        "request_id": "request-1",
        "name": "  洗衣精  ",
        "created_at": datetime(2026, 11, 14, 3, tzinfo=UTC),
        "quantity": None,
        "unit": None,
    }
    values.update(changes)
    return AddShoppingIntent(**values)


def test_add_creates_a_pending_intent_without_inventing_quantity_or_unit():
    client = FakeTransactionalBatchClient()
    service = ShoppingIntentService(CosmosWriteRepository(client))

    result = asyncio.run(service.add(make_command()))

    assert result.status is WriteStatus.APPLIED
    assert result.item.name == "洗衣精"
    assert result.item.quantity is None
    assert result.item.unit is None
    assert result.item.status == "pending"
    assert result.item.created_at == result.item.updated_at
    _, shopping_item_id = resolve_shopping_intent_identity(
        household_id="demo-household", request_id="request-1"
    )
    document = client.documents[("demo-household", f"shopping_item:{shopping_item_id}")]
    assert document["record_type"] == "shopping_item"
    assert document["name"] == "洗衣精"
    assert document["quantity"] is None
    assert document["unit"] is None
    assert document["status"] == "pending"
    operation_id, _ = resolve_shopping_intent_identity(
        household_id="demo-household", request_id="request-1"
    )
    event = client.documents[("demo-household", f"shopping_intent_event:{operation_id}")]
    assert event["record_type"] == "shopping_intent_event"
    assert event["event_id"] == event["operation_id"] == operation_id
    assert event["shopping_item_id"] == shopping_item_id
    assert event["occurred_at"] == "2026-11-14T03:00:00.000000Z"
    assert [operation.document["record_type"] for operation in client.batch_calls[0][1][1:]] == [
        "shopping_item",
        "shopping_intent_event",
    ]
    assert len(client.batch_calls) == 1


def test_add_normalizes_optional_values_and_retries_the_same_operation_id():
    client = FakeTransactionalBatchClient()
    service = ShoppingIntentService(CosmosWriteRepository(client))
    command = make_command(quantity=2, unit="  瓶 ")

    first = asyncio.run(service.add(command))
    retry = asyncio.run(service.add(command))

    assert first.status is WriteStatus.APPLIED
    assert retry.status is WriteStatus.ALREADY_APPLIED
    assert retry.item.quantity == 2
    assert retry.item.unit == "瓶"
    assert len(client.batch_calls) == 2
    assert len(client.documents) == 3  # Shopping row, event, and operation receipt.


def test_add_normalizes_household_consistently_for_identity_documents_and_partition():
    client = FakeTransactionalBatchClient()
    service = ShoppingIntentService(CosmosWriteRepository(client))

    first = asyncio.run(service.add(make_command(household_id=" demo-household ")))
    retry = asyncio.run(service.add(make_command(household_id="demo-household")))

    assert first.status is WriteStatus.APPLIED
    assert retry.status is WriteStatus.ALREADY_APPLIED
    assert first.item.household_id == retry.item.household_id == "demo-household"
    operation_id, shopping_item_id = resolve_shopping_intent_identity(
        household_id="demo-household", request_id="request-1"
    )
    assert [call[0] for call in client.batch_calls] == ["demo-household", "demo-household"]
    shopping = client.documents[("demo-household", f"shopping_item:{shopping_item_id}")]
    event = client.documents[("demo-household", f"shopping_intent_event:{operation_id}")]
    assert shopping["household_id"] == event["household_id"] == "demo-household"
    assert len(client.documents) == 3  # Shopping row, event, and one operation receipt.


def test_add_rejects_changed_payload_under_the_same_operation_identity():
    client = FakeTransactionalBatchClient()
    service = ShoppingIntentService(CosmosWriteRepository(client))
    asyncio.run(service.add(make_command(name="洗衣精")))

    with pytest.raises(OperationIdConflict, match="different canonical values"):
        asyncio.run(service.add(make_command(name="洗碗精")))

    assert len(client.documents) == 3


def test_request_identity_derives_stable_household_scoped_operation_and_item_ids():
    identity = resolve_shopping_intent_identity(
        household_id=" demo-household ", request_id=" request-1 "
    )
    assert identity == resolve_shopping_intent_identity(
        household_id="demo-household", request_id="request-1"
    )
    assert identity[0] != identity[1]
    assert identity != resolve_shopping_intent_identity(
        household_id="other-household", request_id="request-1"
    )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"household_id": "  "}, "household_id"),
        ({"request_id": " "}, "request_id"),
        ({"name": "  "}, "name"),
        ({"quantity": 0}, "quantity"),
        ({"quantity": -1}, "quantity"),
        ({"quantity": True}, "quantity"),
        ({"quantity": 1.5}, "quantity"),
        ({"unit": "  "}, "unit"),
        ({"created_at": datetime(2026, 11, 14, 3)}, "timezone"),
    ],
)
def test_add_rejects_invalid_commands_before_calling_the_writer(changes, message):
    client = FakeTransactionalBatchClient()
    service = ShoppingIntentService(CosmosWriteRepository(client))

    with pytest.raises(ShoppingIntentError, match=message):
        asyncio.run(service.add(make_command(**changes)))

    assert client.batch_calls == []
    assert client.documents == {}

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.models.domain import InventoryItem, ShoppingItem, ShoppingStatus, Transaction
from app.services.cosmos_read import (
    CosmosDocumentMapper,
    CosmosInventoryService,
    CosmosReadRepository,
    CosmosShoppingService,
    MappedDocument,
    TransactionQuery,
    build_transaction_query,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


class FakeDocumentClient:
    def __init__(self, documents):
        self.documents = documents
        self.calls = []
        self.read_calls = []

    async def query_items(self, *, query, parameters, partition_key):
        self.calls.append(
            {
                "query": query,
                "parameters": list(parameters),
                "partition_key": partition_key,
            }
        )
        return self.documents

    async def read_item(self, *, item_id, partition_key):
        self.read_calls.append({"item_id": item_id, "partition_key": partition_key})
        return next(
            (
                document
                for document in self.documents
                if document.get("id") == item_id and document.get("household_id") == partition_key
            ),
            None,
        )


@pytest.mark.parametrize(
    ("model", "record_type", "public_id", "identity_field"),
    [
        (
            InventoryItem(
                item_id="rice",
                household_id="home",
                name="Rice",
                quantity=2,
                unit="bag",
                created_at=NOW,
                updated_at=NOW,
            ),
            "inventory_item",
            "rice",
            "item_id",
        ),
        (
            ShoppingItem(
                shopping_item_id="milk",
                household_id="home",
                name="Milk",
                quantity=1,
                unit="bottle",
                status="pending",
                created_at=NOW,
                updated_at=NOW,
            ),
            "shopping_item",
            "milk",
            "shopping_item_id",
        ),
        (
            Transaction(
                transaction_id="tx-1",
                household_id="home",
                item_id="rice",
                operation_id="op-1",
                quantity_delta=2,
                reason="purchase",
                created_at=NOW,
            ),
            "transaction",
            "tx-1",
            "transaction_id",
        ),
    ],
)
def test_document_mapping_round_trips_domain_record(model, record_type, public_id, identity_field):
    document = CosmosDocumentMapper.to_document(model)

    assert document["id"] == f"{record_type}:{public_id}"
    assert document["record_type"] == record_type
    assert document["household_id"] == "home"
    assert document["created_at"] == "2026-01-01T00:00:00.000000Z"
    assert (
        CosmosDocumentMapper.from_document(
            type(model),
            document | {"_etag": "etag"},
            household_id="home",
        )
        == model
    )
    assert identity_field in document


def test_document_mapping_normalizes_offset_timestamps_to_utc():
    item = InventoryItem(
        item_id="rice",
        household_id="home",
        name="Rice",
        quantity=2,
        unit="bag",
        created_at=datetime.fromisoformat("2026-01-01T08:00:00+08:00"),
        updated_at=datetime.fromisoformat("2026-01-01T08:00:00+08:00"),
    )

    document = CosmosDocumentMapper.to_document(item)

    assert document["created_at"] == "2026-01-01T00:00:00.000000Z"


def test_document_mapping_rejects_naive_timestamps_and_scope_mismatch():
    item = InventoryItem(
        item_id="rice",
        household_id="home",
        name="Rice",
        quantity=2,
        unit="bag",
        created_at=datetime(2026, 1, 1),
        updated_at=NOW,
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        CosmosDocumentMapper.to_document(item)

    document = CosmosDocumentMapper.to_document(
        InventoryItem(
            item_id="rice",
            household_id="home",
            name="Rice",
            quantity=2,
            unit="bag",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    with pytest.raises(ValueError, match="household_id"):
        CosmosDocumentMapper.from_document(
            InventoryItem,
            document,
            household_id="other",
        )


def test_document_mapping_rejects_record_type_and_namespaced_id_mismatch():
    transaction = Transaction(
        transaction_id="tx-1",
        household_id="home",
        item_id="rice",
        operation_id="op-1",
        quantity_delta=2,
        reason="purchase",
        created_at=NOW,
    )
    document = CosmosDocumentMapper.to_document(transaction)

    with pytest.raises(ValueError, match="record_type"):
        CosmosDocumentMapper.from_document(
            Transaction,
            document | {"record_type": "inventory_item"},
            household_id="home",
        )
    with pytest.raises(ValueError, match="namespaced"):
        CosmosDocumentMapper.from_document(
            Transaction,
            document | {"id": "transaction:another"},
            household_id="home",
        )


def test_document_mapping_preserves_cosmos_etag_for_conditional_writes():
    item = InventoryItem(
        item_id="rice",
        household_id="home",
        name="Rice",
        quantity=2,
        unit="bag",
        created_at=NOW,
        updated_at=NOW,
    )
    document = CosmosDocumentMapper.to_document(item) | {"_etag": '"etag-1"'}

    mapped = CosmosDocumentMapper.from_document_with_metadata(
        InventoryItem,
        document,
        household_id="home",
    )

    assert mapped == MappedDocument(
        record=item,
        document_id="inventory_item:rice",
        etag='"etag-1"',
    )


def test_transaction_query_applies_exact_item_since_limit_and_partition():
    since = datetime.fromisoformat("2025-12-01T08:00:00+08:00")
    query = build_transaction_query(
        TransactionQuery(
            household_id="home",
            item_id="rice",
            since=since,
            limit=10,
        )
    )

    assert "c.record_type = @record_type" in query.query
    assert "c.household_id = @household_id" in query.query
    assert "c.item_id = @item_id" in query.query
    assert "c.created_at >= @since" in query.query
    assert "SELECT TOP @limit *" in query.query
    assert "ORDER BY c.created_at DESC, c.transaction_id ASC" in query.query
    assert query.partition_key == "home"
    assert {parameter["name"]: parameter["value"] for parameter in query.parameters} == {
        "@record_type": "transaction",
        "@household_id": "home",
        "@item_id": "rice",
        "@since": "2025-12-01T00:00:00.000000Z",
        "@limit": 10,
    }


def test_cosmos_index_policy_supports_transaction_ordering():
    policy_path = Path(__file__).resolve().parents[2] / "datalake" / "cosmos-indexing-policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))

    assert policy["compositeIndexes"] == [
        [
            {"path": "/created_at", "order": "descending"},
            {"path": "/transaction_id", "order": "ascending"},
        ]
    ]


@pytest.mark.parametrize(
    "values",
    [
        {"household_id": ""},
        {"household_id": "home", "item_id": ""},
        {"household_id": "home", "since": datetime(2026, 1, 1)},
        {"household_id": "home", "limit": 0},
        {"household_id": "home", "limit": -1},
        {"household_id": "home", "limit": True},
        {"household_id": "home", "limit": 1.5},
    ],
)
def test_transaction_query_rejects_invalid_scope_and_filters(values):
    with pytest.raises(ValueError):
        TransactionQuery(**values)


def test_repository_always_uses_partition_key_and_validates_returned_scope():
    transaction = Transaction(
        transaction_id="tx-1",
        household_id="home",
        item_id="rice",
        operation_id="op-1",
        quantity_delta=2,
        reason="purchase",
        created_at=NOW,
    )
    client = FakeDocumentClient([CosmosDocumentMapper.to_document(transaction)])
    repository = CosmosReadRepository(client)

    result = asyncio.run(
        repository.list_transactions(
            TransactionQuery(household_id="home", since=NOW - timedelta(days=1))
        )
    )

    assert result == [transaction]
    assert client.calls[0]["partition_key"] == "home"
    assert any(
        parameter["name"] == "@household_id" and parameter["value"] == "home"
        for parameter in client.calls[0]["parameters"]
    )


def test_inventory_repository_omits_soft_deleted_records_from_default_list():
    item = InventoryItem(
        item_id="rice",
        household_id="home",
        name="Rice",
        quantity=2,
        unit="bag",
        created_at=NOW,
        updated_at=NOW,
    )
    deleted_item = InventoryItem(
        item_id="beans",
        household_id="home",
        name="Beans",
        quantity=1,
        unit="bag",
        created_at=NOW,
        updated_at=NOW,
    )
    deleted_document = CosmosDocumentMapper.to_document(deleted_item) | {
        "deleted_at": "2026-01-02T00:00:00.000000Z"
    }
    item_with_null_marker = InventoryItem(
        item_id="flour",
        household_id="home",
        name="Flour",
        quantity=1,
        unit="bag",
        created_at=NOW,
        updated_at=NOW,
    )
    null_marker_document = CosmosDocumentMapper.to_document(item_with_null_marker) | {
        "deleted_at": None
    }
    client = FakeDocumentClient(
        [CosmosDocumentMapper.to_document(item), deleted_document, null_marker_document]
    )
    repository = CosmosReadRepository(client)

    result = asyncio.run(repository.list_inventory("home"))

    assert result == [item, item_with_null_marker]
    assert client.calls[0]["partition_key"] == "home"
    assert "c.record_type = @record_type" in client.calls[0]["query"]
    assert "c.household_id = @household_id" in client.calls[0]["query"]
    assert "NOT IS_DEFINED(c.deleted_at) OR IS_NULL(c.deleted_at)" in client.calls[0]["query"]
    assert (
        CosmosDocumentMapper.from_document(
            InventoryItem,
            deleted_document,
            household_id="home",
        )
        == deleted_item
    )


def test_service_adapters_match_shared_list_items_interfaces():
    item = InventoryItem(
        item_id="rice",
        household_id="home",
        name="Rice",
        quantity=2,
        unit="bag",
        created_at=NOW,
        updated_at=NOW,
    )
    shopping_item = ShoppingItem(
        shopping_item_id="milk",
        household_id="home",
        name="Milk",
        quantity=1,
        unit="bottle",
        status="pending",
        created_at=NOW,
        updated_at=NOW,
    )
    inventory_repository = CosmosReadRepository(
        FakeDocumentClient([CosmosDocumentMapper.to_document(item)])
    )
    shopping_repository = CosmosReadRepository(
        FakeDocumentClient([CosmosDocumentMapper.to_document(shopping_item)])
    )

    assert asyncio.run(CosmosInventoryService(inventory_repository).list_items("home")) == [item]
    assert asyncio.run(CosmosShoppingService(shopping_repository).list_items("home")) == [
        shopping_item
    ]


def test_shopping_list_hides_completed_and_soft_deleted_items():
    pending = ShoppingItem(
        shopping_item_id="pending-milk",
        household_id="home",
        name="Milk",
        quantity=1,
        unit="bottle",
        status="pending",
        created_at=NOW,
        updated_at=NOW,
    )
    completed = ShoppingItem(
        shopping_item_id="completed-rice",
        household_id="home",
        name="Rice",
        quantity=1,
        unit="bag",
        status="completed",
        created_at=NOW,
        updated_at=NOW,
    )
    removed = ShoppingItem(
        shopping_item_id="removed-tea",
        household_id="home",
        name="Tea",
        quantity=1,
        unit="box",
        status="pending",
        created_at=NOW,
        updated_at=NOW,
    )
    client = FakeDocumentClient(
        [
            CosmosDocumentMapper.to_document(pending),
            CosmosDocumentMapper.to_document(completed),
            CosmosDocumentMapper.to_document(removed)
            | {"deleted_at": "2026-01-02T00:00:00.000000Z"},
        ]
    )
    repository = CosmosReadRepository(client)

    results = asyncio.run(repository.list_shopping("home"))

    assert results == [pending]
    assert "c.status = @pending_status" in client.calls[0]["query"]
    assert "NOT IS_DEFINED(c.deleted_at) OR IS_NULL(c.deleted_at)" in client.calls[0]["query"]
    assert {"name": "@pending_status", "value": "pending"} in client.calls[0]["parameters"]


def test_point_read_hides_soft_deleted_shopping_item_in_its_household():
    item = ShoppingItem(
        shopping_item_id="tea",
        household_id="home",
        name="Tea",
        quantity=1,
        unit="box",
        status="pending",
        created_at=NOW,
        updated_at=NOW,
    )
    client = FakeDocumentClient(
        [CosmosDocumentMapper.to_document(item) | {"deleted_at": "2026-01-02T00:00:00.000000Z"}]
    )
    repository = CosmosReadRepository(client)

    result = asyncio.run(repository.read_shopping_item(household_id="home", shopping_item_id="tea"))

    assert result is None
    assert client.read_calls == [{"item_id": "shopping_item:tea", "partition_key": "home"}]


def test_with_etag_reads_return_etag_and_keep_plain_reads_unchanged():
    inventory = InventoryItem(
        item_id="rice",
        household_id="home",
        name="Rice",
        quantity=2,
        unit="bag",
        created_at=NOW,
        updated_at=NOW,
    )
    shopping = ShoppingItem(
        shopping_item_id="milk",
        household_id="home",
        name="Milk",
        quantity=1,
        unit="bottle",
        status=ShoppingStatus.PENDING,
        created_at=NOW,
        updated_at=NOW,
    )
    deleted = CosmosDocumentMapper.to_document(inventory.model_copy(update={"item_id": "gone"})) | {
        "_etag": '"e3"',
        "deleted_at": "2026-01-02T00:00:00.000000Z",
    }
    client = FakeDocumentClient(
        [
            CosmosDocumentMapper.to_document(inventory) | {"_etag": '"e1"'},
            CosmosDocumentMapper.to_document(shopping) | {"_etag": '"e2"'},
            deleted,
        ]
    )
    repository = CosmosReadRepository(client)
    inventory_only = CosmosReadRepository(FakeDocumentClient([client.documents[0], deleted]))
    shopping_only = CosmosReadRepository(FakeDocumentClient([client.documents[1]]))

    mapped_item = asyncio.run(
        repository.read_inventory_item_with_etag(household_id="home", item_id="rice")
    )
    mapped_shopping = asyncio.run(
        repository.read_shopping_item_with_etag(household_id="home", shopping_item_id="milk")
    )
    listed = asyncio.run(inventory_only.list_inventory_with_etag("home"))
    listed_shopping = asyncio.run(shopping_only.list_shopping_with_etag("home"))

    assert mapped_item is not None and mapped_item.record == inventory
    assert mapped_item.etag == '"e1"'
    assert mapped_shopping is not None and mapped_shopping.etag == '"e2"'
    assert [(m.record.item_id, m.etag) for m in listed] == [("rice", '"e1"')]
    assert [m.etag for m in listed_shopping] == ['"e2"']
    assert asyncio.run(inventory_only.list_inventory("home")) == [inventory]
    assert (
        asyncio.run(repository.read_inventory_item(household_id="home", item_id="rice"))
        == inventory
    )
    assert (
        asyncio.run(repository.read_inventory_item_with_etag(household_id="home", item_id="gone"))
        is None
    )

"""Firestore-backed direct shopping-add regression coverage without live data."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime

from identity_test_support import authenticated_client

from app.core import dependencies
from app.core.dependencies import get_shopping_service
from app.main import create_app
from app.services.cosmos_read import CosmosReadRepository, CosmosShoppingService
from app.services.firestore_sdk import FirestoreTransactionalBatchClient, FirestoreWriteRepository

NOW = datetime(2026, 11, 20, 10, tzinfo=UTC)


class FakeSnapshot:
    def __init__(self, document):
        self.exists = document is not None
        self._document = deepcopy(document)
        self.update_time = NOW

    def to_dict(self):
        return deepcopy(self._document)


class FakeDocumentReference:
    def __init__(self, client, path):
        self._client = client
        self.path = path

    def get(self):
        return FakeSnapshot(self._client.documents.get(self.path))


class FakeTransaction:
    def __init__(self, client):
        self._client = client
        self._writes = []

    def get(self, reference):
        return FakeSnapshot(self._client.documents.get(reference.path))

    def create(self, reference, document):
        self._writes.append((reference.path, deepcopy(document), True))

    def set(self, reference, document):
        self._writes.append((reference.path, deepcopy(document), False))

    def commit(self):
        if any(create and path in self._client.documents for path, _, create in self._writes):
            raise AssertionError("Fake transaction must check create preconditions before commit.")
        for path, document, _ in self._writes:
            self._client.documents[path] = document


class FakeFirestoreClient:
    def __init__(self):
        self.documents = {}

    def document(self, path):
        return FakeDocumentReference(self, path)


class FakeShoppingQueryClient:
    def __init__(self, client):
        self._client = client

    async def read_item(self, *, item_id, partition_key):
        return await FirestoreTransactionalBatchClient(self._client).read_item(
            item_id=item_id,
            partition_key=partition_key,
        )

    async def query_items(self, *, query, parameters, partition_key):
        del query, parameters
        prefix = f"households/{partition_key}/records/"
        return [
            deepcopy(document)
            for path, document in self._client.documents.items()
            if path.startswith(prefix)
            and document.get("household_id") == partition_key
            and document.get("record_type") == "shopping_item"
            and document.get("status") == "pending"
            and document.get("deleted_at") is None
        ]


def test_firestore_post_persists_for_get_and_reuses_request_id(monkeypatch):
    import app.services.firestore_sdk as firestore_sdk

    client = FakeFirestoreClient()
    query = FakeShoppingQueryClient(client)
    batch = FirestoreTransactionalBatchClient(client)
    read_repository = CosmosReadRepository(query)
    firestore_repository = FirestoreWriteRepository(
        client,
        query_client=query,
        transactional_client=batch,
    )

    def run_transaction(fake_client, callback, *, max_attempts=5):
        del max_attempts
        transaction = FakeTransaction(fake_client)
        callback(transaction)
        transaction.commit()

    monkeypatch.setattr(firestore_sdk, "_run_transaction", run_transaction)
    monkeypatch.setattr(dependencies, "cosmos_read_repository", read_repository)
    monkeypatch.setattr(dependencies, "cosmos_write_repository", firestore_repository)
    monkeypatch.setattr(dependencies, "cosmos_document_reader", query)

    app = create_app()
    app.dependency_overrides[get_shopping_service] = lambda: CosmosShoppingService(
        read_repository
    )
    with authenticated_client(app) as api:
        body = {
            "request_id": "explicit-add-001",
            "name": "  洗衣精 ",
            "quantity": 2,
            "unit": "瓶",
        }
        added = api.post("/shopping", json=body)
        retry = api.post("/shopping", json=body)
        reloaded = api.get("/shopping")
        conflicting_retry = api.post("/shopping", json={**body, "quantity": 3})
        shopping_path = next(
            path
            for path, document in client.documents.items()
            if document.get("record_type") == "shopping_item"
        )
        # Model a separate approved completion; an Add retry must not reopen it.
        client.documents[shopping_path]["status"] = "completed"
        completed_retry = api.post("/shopping", json=body)
        completed_list = api.get("/shopping")

    assert added.status_code == 201
    assert added.json()["name"] == body["name"]
    assert added.json()["quantity"] == body["quantity"]
    assert added.json()["unit"] == body["unit"]
    assert added.json()["status"] == "pending"
    assert retry.status_code == 200
    assert retry.json() == added.json()
    assert reloaded.status_code == 200
    assert reloaded.json() == [added.json()]
    assert conflicting_retry.status_code == 409
    assert completed_retry.status_code == 200
    assert completed_list.json() == []
    assert client.documents[shopping_path]["status"] == "completed"

    stored = list(client.documents.values())
    shopping_records = [
        document for document in stored if document.get("record_type") == "shopping_item"
    ]
    receipt_records = [
        document for document in stored if document.get("record_type") == "operation_receipt"
    ]
    assert len(shopping_records) == 1
    assert len(receipt_records) == 1
    assert shopping_records[0]["household_id"] == "demo-household"
    assert shopping_records[0]["status"] == "completed"
    assert not any("price" in key.lower() for document in stored for key in document)
    assert retry.status_code == 200 and len(stored) == 2
    assert asyncio.run(read_repository.list_shopping("another-household")) == []

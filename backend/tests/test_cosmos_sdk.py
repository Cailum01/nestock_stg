import asyncio
import logging

import pytest
from azure.cosmos.exceptions import CosmosBatchOperationError, CosmosResourceNotFoundError

from app.core import dependencies
from app.core.dependencies import _create_domain_read_services, _create_read_composition
from app.models.domain import InventoryItem, ShoppingItem
from app.services import cosmos_sdk
from app.services.cosmos_read import CosmosDocumentMapper, CosmosReadRepository
from app.services.cosmos_sdk import (
    COSMOS_TRANSACTIONAL_BATCH_LIMIT,
    AzureCosmosDocumentQueryClient,
    AzureCosmosTransactionalBatchClient,
    create_azure_cosmos_read_repository,
    create_azure_cosmos_repositories,
)
from app.services.cosmos_write import BatchOperationConflict, CreateDocument, ReplaceDocument
from app.services.demo import DemoInventoryService, DemoShoppingService

NOW = "2026-01-01T00:00:00.000000Z"


class FakeContainer:
    def __init__(self, documents):
        self.documents = documents
        self.calls = []
        self.point_read_calls = []

    def query_items(self, *, query, parameters, partition_key, response_hook):
        self.calls.append(
            {
                "query": query,
                "parameters": parameters,
                "partition_key": partition_key,
            }
        )
        response_hook({"x-ms-request-charge": "1.25"}, {})
        response_hook({"X-MS-REQUEST-CHARGE": "0.75"}, {})
        return iter(self.documents)

    def read_item(self, *, item, partition_key, response_hook):
        self.point_read_calls.append({"item": item, "partition_key": partition_key})
        for document in self.documents:
            if document["id"] == item and document["household_id"] == partition_key:
                response_hook({"x-ms-request-charge": "0.9"}, {})
                return document
        raise CosmosResourceNotFoundError(status_code=404)


class FakeWritableContainer(FakeContainer):
    def __init__(self, documents):
        super().__init__(documents)
        self.batch_calls = []
        self.batch_error = None

    def execute_item_batch(self, *, batch_operations, partition_key, response_hook):
        self.batch_calls.append(
            {"batch_operations": batch_operations, "partition_key": partition_key}
        )
        if self.batch_error is not None:
            raise self.batch_error
        response_hook({"x-ms-request-charge": "1.5"}, [])
        return []


def test_sdk_batch_client_maps_create_and_etag_replace_operations():
    container = FakeWritableContainer([])
    client = AzureCosmosTransactionalBatchClient(container)

    asyncio.run(
        client.execute_batch(
            partition_key="home",
            operations=(
                CreateDocument(
                    {
                        "id": "inventory:rice",
                        "record_type": "inventory_item",
                        "household_id": "home",
                    }
                ),
                ReplaceDocument(
                    document={
                        "id": "pending_approval:approval-1",
                        "record_type": "pending_approval",
                        "household_id": "home",
                        "status": "confirmed",
                        "_etag": '"etag-1"',
                        "_ts": 123,
                    },
                    if_match_etag='"etag-1"',
                ),
            ),
        )
    )

    assert container.batch_calls == [
        {
            "partition_key": "home",
            "batch_operations": [
                (
                    "create",
                    (
                        {
                            "id": "inventory:rice",
                            "record_type": "inventory_item",
                            "household_id": "home",
                        },
                    ),
                ),
                (
                    "replace",
                    (
                        "pending_approval:approval-1",
                        {
                            "id": "pending_approval:approval-1",
                            "record_type": "pending_approval",
                            "household_id": "home",
                            "status": "confirmed",
                        },
                    ),
                    {"if_match_etag": '"etag-1"'},
                ),
            ],
        }
    ]


def test_sdk_batch_client_logs_write_metrics_without_household_or_document_data(caplog):
    container = FakeWritableContainer([])
    client = AzureCosmosTransactionalBatchClient(container)

    with caplog.at_level(logging.INFO, logger="app.services.cosmos_sdk"):
        asyncio.run(
            client.execute_batch(
                partition_key="private-household",
                operations=(
                    CreateDocument(
                        {
                            "id": "inventory:private-item",
                            "record_type": "inventory_item",
                            "household_id": "private-household",
                        }
                    ),
                ),
            )
        )

    assert "request_charge=1.5" in caplog.text
    assert "operation_count=1" in caplog.text
    assert "private-household" not in caplog.text
    assert "private-item" not in caplog.text


@pytest.mark.parametrize("status_code", [409, 412])
def test_sdk_batch_client_maps_conflict_errors_with_operation_index(status_code):
    container = FakeWritableContainer([])
    container.batch_error = CosmosBatchOperationError(
        error_index=2,
        headers={},
        status_code=status_code,
        message="Batch conflict",
    )
    client = AzureCosmosTransactionalBatchClient(container)

    with pytest.raises(BatchOperationConflict) as error:
        asyncio.run(
            client.execute_batch(
                partition_key="home",
                operations=(CreateDocument({"id": "record:1", "household_id": "home"}),),
            )
        )

    assert error.value.operation_index == 2


def test_sdk_batch_client_propagates_non_conflict_errors():
    container = FakeWritableContainer([])
    error = CosmosBatchOperationError(
        error_index=0,
        headers={},
        status_code=429,
        message="Throttled",
    )
    container.batch_error = error
    client = AzureCosmosTransactionalBatchClient(container)

    with pytest.raises(CosmosBatchOperationError) as raised:
        asyncio.run(
            client.execute_batch(
                partition_key="home",
                operations=(CreateDocument({"id": "record:1", "household_id": "home"}),),
            )
        )

    assert raised.value is error


@pytest.mark.parametrize(
    ("partition_key", "operations", "message"),
    [
        ("", (CreateDocument({"id": "record:1", "household_id": "home"}),), "partition_key"),
        ("home", (), "at least one operation"),
        (
            "home",
            (CreateDocument({"id": "record:1", "household_id": "other"}),),
            "household partition",
        ),
        ("home", (CreateDocument({"household_id": "home"}),), "non-empty Cosmos id"),
        (
            "home",
            (
                ReplaceDocument(
                    document={"id": "record:1", "household_id": "home"},
                    if_match_etag="",
                ),
            ),
            "current Cosmos _etag",
        ),
    ],
)
def test_sdk_batch_client_rejects_invalid_batch_operations(partition_key, operations, message):
    container = FakeWritableContainer([])
    client = AzureCosmosTransactionalBatchClient(container)

    with pytest.raises(ValueError, match=message):
        asyncio.run(client.execute_batch(partition_key=partition_key, operations=operations))

    assert container.batch_calls == []


def test_sdk_batch_client_enforces_cosmos_operation_limit():
    container = FakeWritableContainer([])
    client = AzureCosmosTransactionalBatchClient(container)
    operations = tuple(
        CreateDocument({"id": f"record:{index}", "household_id": "home"})
        for index in range(COSMOS_TRANSACTIONAL_BATCH_LIMIT + 1)
    )

    with pytest.raises(ValueError, match="cannot exceed 100 operations"):
        asyncio.run(client.execute_batch(partition_key="home", operations=operations))

    assert container.batch_calls == []


def test_sdk_batch_client_accepts_exact_cosmos_operation_limit():
    container = FakeWritableContainer([])
    client = AzureCosmosTransactionalBatchClient(container)
    operations = tuple(
        CreateDocument({"id": f"record:{index}", "household_id": "home"})
        for index in range(COSMOS_TRANSACTIONAL_BATCH_LIMIT)
    )

    asyncio.run(client.execute_batch(partition_key="home", operations=operations))

    assert len(container.batch_calls[0]["batch_operations"]) == COSMOS_TRANSACTIONAL_BATCH_LIMIT


def test_sdk_batch_client_delegates_household_point_reads():
    document = {"id": "pending_approval:approval-1", "household_id": "home"}
    container = FakeWritableContainer([document])
    client = AzureCosmosTransactionalBatchClient(container)

    result = asyncio.run(
        client.read_item(item_id="pending_approval:approval-1", partition_key="home")
    )

    assert result == document
    assert container.point_read_calls == [
        {"item": "pending_approval:approval-1", "partition_key": "home"}
    ]


def test_sdk_query_client_uses_household_partition_and_returns_documents():
    document = {"id": "inventory_item:rice", "household_id": "home"}
    container = FakeContainer([document])
    client = AzureCosmosDocumentQueryClient(container)

    result = asyncio.run(
        client.query_items(
            query="SELECT * FROM c WHERE c.household_id = @household_id",
            parameters=({"name": "@household_id", "value": "home"},),
            partition_key="home",
        )
    )

    assert result == [document]
    assert container.calls == [
        {
            "query": "SELECT * FROM c WHERE c.household_id = @household_id",
            "parameters": [{"name": "@household_id", "value": "home"}],
            "partition_key": "home",
        }
    ]


def test_sdk_query_client_logs_aggregate_charge_without_query_scope_or_document_data(caplog):
    container = FakeContainer([{"id": "inventory_item:rice", "household_id": "home"}])
    client = AzureCosmosDocumentQueryClient(container)

    with caplog.at_level(logging.INFO, logger="app.services.cosmos_sdk"):
        asyncio.run(
            client.query_items(
                query="SELECT * FROM c WHERE c.household_id = @household_id",
                parameters=({"name": "@household_id", "value": "private-household"},),
                partition_key="private-household",
            )
        )

    assert "request_charge=2.0" in caplog.text
    assert "result_count=1" in caplog.text
    assert "duration_ms=" in caplog.text
    assert "private-household" not in caplog.text
    assert "inventory_item:rice" not in caplog.text


def test_repository_reads_inventory_item_by_namespaced_id_and_household_partition(caplog):
    item = InventoryItem(
        item_id="rice",
        household_id="home",
        name="Rice",
        quantity=2,
        unit="bag",
        created_at=NOW,
        updated_at=NOW,
    )
    container = FakeContainer([CosmosDocumentMapper.to_document(item)])
    repository = CosmosReadRepository(AzureCosmosDocumentQueryClient(container))

    with caplog.at_level(logging.INFO, logger="app.services.cosmos_sdk"):
        result = asyncio.run(repository.read_inventory_item(household_id="home", item_id="rice"))

    assert result == item
    assert container.point_read_calls == [{"item": "inventory_item:rice", "partition_key": "home"}]
    assert "Cosmos read point_read completed: request_charge=0.9 result_count=1" in caplog.text
    assert "home" not in caplog.text
    assert "inventory_item:rice" not in caplog.text


def test_repository_treats_soft_deleted_inventory_point_read_as_missing():
    item = InventoryItem(
        item_id="rice",
        household_id="home",
        name="Rice",
        quantity=2,
        unit="bag",
        created_at=NOW,
        updated_at=NOW,
    )
    deleted_document = CosmosDocumentMapper.to_document(item) | {
        "deleted_at": "2026-01-02T00:00:00.000000Z"
    }
    container = FakeContainer([deleted_document])
    repository = CosmosReadRepository(AzureCosmosDocumentQueryClient(container))

    result = asyncio.run(repository.read_inventory_item(household_id="home", item_id="rice"))

    assert result is None
    assert container.point_read_calls == [{"item": "inventory_item:rice", "partition_key": "home"}]


def test_repository_reads_shopping_item_and_returns_none_for_missing_or_other_household():
    item = ShoppingItem(
        shopping_item_id="milk",
        household_id="home",
        name="Milk",
        quantity=1,
        unit="bottle",
        status="pending",
        created_at=NOW,
        updated_at=NOW,
    )
    container = FakeContainer([CosmosDocumentMapper.to_document(item)])
    repository = CosmosReadRepository(AzureCosmosDocumentQueryClient(container))

    result = asyncio.run(
        repository.read_shopping_item(household_id="home", shopping_item_id="milk")
    )
    missing = asyncio.run(
        repository.read_shopping_item(household_id="home", shopping_item_id="unknown")
    )
    other_household = asyncio.run(
        repository.read_shopping_item(household_id="other", shopping_item_id="milk")
    )

    assert result == item
    assert missing is None
    assert other_household is None
    assert container.point_read_calls == [
        {"item": "shopping_item:milk", "partition_key": "home"},
        {"item": "shopping_item:unknown", "partition_key": "home"},
        {"item": "shopping_item:milk", "partition_key": "other"},
    ]


def test_sdk_point_read_logs_not_found_request_charge_from_exception_headers(caplog):
    class NotFoundContainer:
        def read_item(self, **kwargs):
            error = CosmosResourceNotFoundError(status_code=404)
            error.headers = {"x-ms-request-charge": "1.1"}
            raise error

    client = AzureCosmosDocumentQueryClient(NotFoundContainer())

    with caplog.at_level(logging.INFO, logger="app.services.cosmos_sdk"):
        result = asyncio.run(
            client.read_item(item_id="inventory_item:missing", partition_key="home")
        )

    assert result is None
    assert "request_charge=1.1 result_count=0" in caplog.text


def test_sdk_point_read_marks_missing_not_found_request_charge_unavailable(caplog):
    container = FakeContainer([])
    client = AzureCosmosDocumentQueryClient(container)

    with caplog.at_level(logging.INFO, logger="app.services.cosmos_sdk"):
        result = asyncio.run(
            client.read_item(item_id="inventory_item:missing", partition_key="home")
        )

    assert result is None
    assert "request_charge=unavailable result_count=0" in caplog.text


def test_sdk_point_read_propagates_non_not_found_errors():
    class FailingContainer:
        def read_item(self, **kwargs):
            raise RuntimeError("Cosmos point read failed")

    client = AzureCosmosDocumentQueryClient(FailingContainer())

    with pytest.raises(RuntimeError, match="Cosmos point read failed"):
        asyncio.run(client.read_item(item_id="inventory_item:rice", partition_key="home"))


@pytest.mark.parametrize(
    ("household_id", "item_id"),
    [("", "rice"), ("home", "")],
)
def test_point_read_rejects_empty_scope_or_id(household_id, item_id):
    container = FakeContainer([])
    repository = CosmosReadRepository(AzureCosmosDocumentQueryClient(container))

    with pytest.raises(ValueError):
        asyncio.run(repository.read_inventory_item(household_id=household_id, item_id=item_id))

    assert container.point_read_calls == []


def test_sdk_query_client_propagates_sdk_errors():
    class FailingContainer:
        def query_items(self, **kwargs):
            raise RuntimeError("Cosmos query failed")

    client = AzureCosmosDocumentQueryClient(FailingContainer())

    with pytest.raises(RuntimeError, match="Cosmos query failed"):
        asyncio.run(
            client.query_items(query="SELECT * FROM c", parameters=(), partition_key="home")
        )


def test_repository_factory_rejects_non_https_endpoint():
    with pytest.raises(ValueError, match="HTTPS"):
        create_azure_cosmos_read_repository(
            endpoint="http://localhost:8081",
            database_name="nestock",
            container_name="household-data",
        )


def test_repository_factory_shares_container_and_sdk_client_between_repositories(monkeypatch):
    class FakeCredential:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class FakeCosmosClient:
        def __init__(self, container):
            self.container = container
            self.closed = False

        def get_database_client(self, database_name):
            assert database_name == "nestock"
            return self

        def get_container_client(self, container_name):
            assert container_name == "household-data"
            return self.container

        def close(self):
            self.closed = True

    credential = FakeCredential()
    container = FakeWritableContainer([])
    sdk_client = FakeCosmosClient(container)
    monkeypatch.setattr(cosmos_sdk, "DefaultAzureCredential", lambda: credential)
    monkeypatch.setattr(cosmos_sdk, "CosmosClient", lambda endpoint, credential: sdk_client)

    read_repository, write_repository, close = create_azure_cosmos_repositories(
        endpoint="https://example.documents.azure.com",
        database_name="nestock",
        container_name="household-data",
    )

    assert isinstance(read_repository, CosmosReadRepository)
    assert read_repository._client._container is container
    assert write_repository._client._container is container
    close()
    assert sdk_client.closed
    assert credential.closed


def test_missing_cosmos_configuration_keeps_offline_demo_services():
    inventory, shopping = _create_domain_read_services({})

    assert isinstance(inventory, DemoInventoryService)
    assert isinstance(shopping, DemoShoppingService)
    assert _create_read_composition({})[2] is None


def test_cosmos_read_services_share_repository_with_composition(monkeypatch):
    repository = CosmosReadRepository(FakeContainer([]))
    monkeypatch.setattr(
        dependencies,
        "create_azure_cosmos_read_repository",
        lambda **kwargs: (repository, lambda: None),
    )

    inventory, shopping, shared_repository = _create_read_composition(
        {
            "NESTOCK_COSMOS_ENDPOINT": "https://example.documents.azure.com",
            "NESTOCK_COSMOS_DATABASE": "nestock",
            "NESTOCK_COSMOS_CONTAINER": "household-data",
        }
    )

    assert shared_repository is repository
    assert inventory._repository is repository
    assert shopping._repository is repository


def test_partial_cosmos_configuration_fails_explicitly():
    with pytest.raises(RuntimeError, match="NESTOCK_COSMOS_DATABASE"):
        _create_domain_read_services(
            {"NESTOCK_COSMOS_ENDPOINT": "https://example.documents.azure.com"}
        )


def test_empty_cosmos_configuration_fails_instead_of_falling_back():
    with pytest.raises(RuntimeError, match="must not be empty"):
        _create_domain_read_services(
            {
                "NESTOCK_COSMOS_ENDPOINT": "",
                "NESTOCK_COSMOS_DATABASE": "nestock",
                "NESTOCK_COSMOS_CONTAINER": "household-data",
            }
        )

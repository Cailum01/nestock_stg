import asyncio
import logging

import pytest

from app.core.dependencies import _create_domain_read_services
from app.models.domain import InventoryItem, ShoppingItem
from app.services.cosmos_read import CosmosDocumentMapper, CosmosReadRepository
from app.services.cosmos_sdk import (
    AzureCosmosDocumentQueryClient,
    create_azure_cosmos_read_repository,
)
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
        response_hook({"x-ms-request-charge": "0.9"}, {})
        for document in self.documents:
            if document["id"] == item and document["household_id"] == partition_key:
                return document
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        raise CosmosResourceNotFoundError(status_code=404)


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


def test_missing_cosmos_configuration_keeps_offline_demo_services():
    inventory, shopping = _create_domain_read_services({})

    assert isinstance(inventory, DemoInventoryService)
    assert isinstance(shopping, DemoShoppingService)


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

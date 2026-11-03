import asyncio
import logging

import pytest

from app.core.dependencies import _create_domain_read_services
from app.services.cosmos_sdk import (
    AzureCosmosDocumentQueryClient,
    create_azure_cosmos_read_repository,
)
from app.services.demo import DemoInventoryService, DemoShoppingService


class FakeContainer:
    def __init__(self, documents):
        self.documents = documents
        self.calls = []

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

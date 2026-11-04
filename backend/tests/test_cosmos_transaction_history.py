import asyncio
import logging
from datetime import UTC, datetime, timedelta

import pytest

from app.models.domain import InventoryItem, Transaction, TransactionReason
from app.models.forecast import ForecastMethod, ForecastStatus
from app.services.cosmos_read import CosmosDocumentMapper, CosmosReadRepository
from app.services.cosmos_transaction_history import (
    COSMOS_SETTINGS,
    CosmosTransactionHistory,
    create_transaction_history,
)
from app.services.forecast import ForecastService
from app.services.interfaces import TransactionHistory
from app.services.transaction_history import DemoTransactionHistory

NOW = datetime(2026, 11, 4, tzinfo=UTC)
HOUSEHOLD = "demo-household"


class FakeDocumentClient:
    def __init__(self, documents):
        self.documents = documents
        self.calls = []

    async def query_items(self, *, query, parameters, partition_key):
        self.calls.append(
            {"query": query, "parameters": list(parameters), "partition_key": partition_key}
        )
        return self.documents


class FakeInventory:
    async def list_items(self, household_id):
        return [
            InventoryItem(
                item_id="rice",
                household_id=household_id,
                name="白米",
                quantity=2,
                unit="包",
                location="廚房",
                created_at=NOW - timedelta(days=200),
                updated_at=NOW - timedelta(days=1),
            )
        ]


def purchase(days_ago: int) -> Transaction:
    return Transaction(
        transaction_id=f"tx-{days_ago}",
        household_id=HOUSEHOLD,
        item_id="rice",
        operation_id=f"op-{days_ago}",
        quantity_delta=2,
        reason=TransactionReason.PURCHASE,
        created_at=NOW - timedelta(days=days_ago),
    )


def history_over(records: list[Transaction]) -> tuple[CosmosTransactionHistory, FakeDocumentClient]:
    client = FakeDocumentClient([CosmosDocumentMapper.to_document(r) for r in records])
    return CosmosTransactionHistory(CosmosReadRepository(client)), client


def test_cosmos_history_conforms_to_port_and_queries_one_household_partition():
    service, client = history_over([purchase(30), purchase(75)])
    history: TransactionHistory = service

    records = asyncio.run(history.list_transactions(HOUSEHOLD))

    assert [r.transaction_id for r in records] == ["tx-30", "tx-75"]
    (call,) = client.calls
    assert call["partition_key"] == HOUSEHOLD
    assert "ORDER BY c.created_at DESC, c.transaction_id ASC" in call["query"]
    # The whole household history: no item, since or limit filter.
    assert {p["name"] for p in call["parameters"]} == {"@record_type", "@household_id"}


def test_cosmos_history_rejects_records_from_another_household():
    other = purchase(30).model_copy(update={"household_id": "other-household"})
    service, _ = history_over([other])

    with pytest.raises(ValueError):
        asyncio.run(service.list_transactions(HOUSEHOLD))


def test_forecast_runs_on_cosmos_history():
    service, _ = history_over([purchase(30), purchase(75), purchase(120)])

    (forecast,) = asyncio.run(
        ForecastService(FakeInventory(), service).forecast_household(HOUSEHOLD, NOW)
    )

    assert forecast.status == ForecastStatus.ESTIMATED
    assert forecast.method == ForecastMethod.PURCHASE_INTERVAL
    assert forecast.confirmed_quantity == 2


def test_no_cosmos_settings_falls_back_to_demo_with_setup_hint(caplog):
    with caplog.at_level(logging.WARNING):
        history = create_transaction_history({})

    assert isinstance(history, DemoTransactionHistory)
    assert "az login" in caplog.text
    assert all(name in caplog.text for name in COSMOS_SETTINGS)


def test_partial_or_empty_cosmos_settings_fail_fast():
    with pytest.raises(RuntimeError, match="NESTOCK_COSMOS_CONTAINER"):
        create_transaction_history(
            {"NESTOCK_COSMOS_ENDPOINT": "https://example", "NESTOCK_COSMOS_DATABASE": "nestock"}
        )
    with pytest.raises(RuntimeError, match="must not be empty"):
        create_transaction_history({name: "" for name in COSMOS_SETTINGS})


def test_full_cosmos_settings_build_cosmos_history(monkeypatch):
    calls = []

    def fake_factory(**kwargs):
        calls.append(kwargs)
        return CosmosReadRepository(FakeDocumentClient([])), lambda: None

    monkeypatch.setattr("app.services.cosmos_sdk.create_azure_cosmos_read_repository", fake_factory)
    history = create_transaction_history(
        {
            "NESTOCK_COSMOS_ENDPOINT": "https://example.documents.azure.com:443/",
            "NESTOCK_COSMOS_DATABASE": "nestock",
            "NESTOCK_COSMOS_CONTAINER": "household-data",
        }
    )

    assert isinstance(history, CosmosTransactionHistory)
    assert calls == [
        {
            "endpoint": "https://example.documents.azure.com:443/",
            "database_name": "nestock",
            "container_name": "household-data",
        }
    ]


def test_shared_repository_is_used_without_settings_or_a_second_client(monkeypatch):
    def no_second_client(**kwargs):
        raise AssertionError("must reuse B1's repository")

    monkeypatch.setattr(
        "app.services.cosmos_sdk.create_azure_cosmos_read_repository", no_second_client
    )
    client = FakeDocumentClient([CosmosDocumentMapper.to_document(purchase(30))])

    history = create_transaction_history({}, repository=CosmosReadRepository(client))

    assert isinstance(history, CosmosTransactionHistory)
    assert [r.transaction_id for r in asyncio.run(history.list_transactions(HOUSEHOLD))] == [
        "tx-30"
    ]

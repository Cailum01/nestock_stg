from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.forecast import get_clock, get_forecast_service, router
from app.core.config import settings
from app.models.domain import InventoryItem, Transaction, TransactionReason
from app.services.forecast import ForecastService, ForecastSettings

NOW = datetime(2026, 11, 12, tzinfo=UTC)
HOUSEHOLD = settings.demo_household_id


class FakeInventory:
    def __init__(self, items: list[InventoryItem]) -> None:
        self._items = items

    async def list_items(self, household_id: str) -> list[InventoryItem]:
        return self._items


class FakeHistory:
    def __init__(self, records: list[Transaction]) -> None:
        self._records = records

    async def list_transactions(self, household_id: str) -> list[Transaction]:
        return self._records


def item(item_id: str, quantity: float) -> InventoryItem:
    return InventoryItem(
        item_id=item_id,
        household_id=HOUSEHOLD,
        name=item_id,
        quantity=quantity,
        unit="包",
        created_at=NOW - timedelta(days=200),
        updated_at=NOW,
    )


def consumption(item_id: str, days_ago: float, quantity: float, n: int) -> Transaction:
    return Transaction(
        transaction_id=f"tx-{item_id}-{n}",
        household_id=HOUSEHOLD,
        item_id=item_id,
        operation_id=f"op-{item_id}-{n}",
        quantity_delta=-quantity,
        reason=TransactionReason.CONSUMPTION,
        created_at=NOW - timedelta(days=days_ago),
    )


def client(items, records, *, confirmed: bool = True) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_clock] = lambda: lambda: NOW
    app.dependency_overrides[get_forecast_service] = lambda: ForecastService(
        FakeInventory(items),
        FakeHistory(records),
        ForecastSettings(time_semantics_confirmed=confirmed),
    )
    return TestClient(app)


# Quantities are whole numbers. Daily use of "a" and "b" is 0.3, so "a" has about 7 days left,
# "b" about 3 days, "c" about 40 days; "d" has no history.
ITEMS = [item("a", 2), item("b", 1), item("c", 8), item("d", 5)]
RECORDS = [
    consumption("a", 20, 3, 1),
    consumption("a", 10, 3, 2),
    consumption("b", 20, 3, 1),
    consumption("b", 10, 3, 2),
    consumption("c", 60, 6, 1),
    consumption("c", 30, 6, 2),
]


def test_reorder_lists_only_restock_items_soonest_first():
    with client(ITEMS, RECORDS) as c:
        response = c.get("/forecast/reorder")

    assert response.status_code == 200
    body = response.json()
    assert [f["item_id"] for f in body] == ["b", "a"]
    assert all(f["suggested_action"] == "consider_restock" for f in body)
    assert body[0]["days_until_empty"] < body[1]["days_until_empty"]


def test_reorder_is_empty_while_time_semantics_are_unconfirmed():
    with client(ITEMS, RECORDS, confirmed=False) as c:
        response = c.get("/forecast/reorder")

    assert response.status_code == 200
    assert response.json() == []


def test_reorder_is_empty_when_nothing_is_running_low():
    with client([item("c", 8)], RECORDS) as c:
        assert c.get("/forecast/reorder").json() == []


def test_reorder_is_not_read_as_an_item_id():
    with client(ITEMS, RECORDS) as c:
        assert c.get("/forecast/reorder").status_code == 200
        assert c.get("/forecast/missing").status_code == 404


def test_reorder_does_not_change_inventory():
    items = [item("a", 1)]
    with client(items, RECORDS) as c:
        c.get("/forecast/reorder")

    assert items[0].quantity == 1


def test_reorder_is_read_only():
    with client(ITEMS, RECORDS) as c:
        assert c.post("/forecast/reorder").status_code == 405

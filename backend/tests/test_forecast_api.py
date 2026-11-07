import asyncio
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.forecast import get_clock, get_forecast_service, router
from app.core.config import settings
from app.core.dependencies import get_inventory_service
from app.services.demo import DemoInventoryService
from app.services.forecast import ForecastService, ForecastSettings
from app.services.transaction_history import DemoTransactionHistory

NOW = datetime(2026, 11, 2, tzinfo=UTC)


def client(*, confirmed: bool = False) -> TestClient:
    # Mounted on its own app; registering in app/main.py belongs to the shared-file owners.
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_clock] = lambda: lambda: NOW
    if confirmed:
        # As if the owners had confirmed that created_at is the business event time.
        inventory_service = DemoInventoryService()
        app.dependency_overrides[get_forecast_service] = lambda: ForecastService(
            inventory_service,
            DemoTransactionHistory(clock=lambda: NOW),
            ForecastSettings(time_semantics_confirmed=True),
        )
    return TestClient(app)


def inventory_items():
    return asyncio.run(get_inventory_service().list_items(settings.demo_household_id))


def test_item_forecast_is_paused_until_created_at_semantics_are_confirmed():
    items = inventory_items()
    assert items
    item = items[0]

    with client() as c:
        response = c.get(f"/forecast/{item.item_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "insufficient_data"
    assert body["suggested_action"] == "none"
    assert body["method"] is None
    assert body["daily_consumption"] is None
    assert body["days_until_empty"] is None
    assert body["confirmed_quantity"] == item.quantity
    assert body["purchase_event_count"] >= 0
    assert body["generated_at"].startswith("2026-11-02")


def test_item_forecast_uses_demo_purchase_history_once_confirmed():
    with client(confirmed=True) as c:
        response = c.get("/forecast/demo-rice")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "estimated"
    assert body["method"] == "purchase_interval"
    assert body["confirmed_quantity"] == 2
    assert round(body["days_until_empty"]) == 45
    assert body["generated_at"].startswith("2026-11-02")


def test_unknown_item_is_404():
    with client() as c:
        assert c.get("/forecast/__nestock_missing_forecast_test_item__").status_code == 404


def test_household_forecast_lists_every_active_item():
    inventory_ids = {item.item_id for item in inventory_items()}

    with client() as c:
        forecasts = c.get("/forecast").json()

    assert {forecast["item_id"] for forecast in forecasts} == {item_id for item_id in inventory_ids}


def test_forecast_routes_are_read_only():
    with client() as c:
        assert c.post("/forecast/demo-rice").status_code == 405
        assert c.get("/openapi.json").json()["paths"]["/forecast/{item_id}"].keys() == {"get"}

from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.forecast import get_clock, get_transaction_history, router
from app.services.transaction_history import DemoTransactionHistory

NOW = datetime(2026, 11, 2, tzinfo=UTC)


def client() -> TestClient:
    # Mounted on its own app; registering in app/main.py belongs to the shared-file owners.
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_clock] = lambda: lambda: NOW
    app.dependency_overrides[get_transaction_history] = lambda: DemoTransactionHistory(
        clock=lambda: NOW
    )
    return TestClient(app)


def test_item_forecast_uses_demo_purchase_history():
    with client() as c:
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
        assert c.get("/forecast/not-an-item").status_code == 404


def test_household_forecast_lists_every_active_item():
    with client() as c:
        body = c.get("/forecast").json()

    assert [f["item_id"] for f in body] == ["demo-rice"]


def test_forecast_routes_are_read_only():
    with client() as c:
        assert c.post("/forecast/demo-rice").status_code == 405
        assert c.get("/openapi.json").json()["paths"]["/forecast/{item_id}"].keys() == {"get"}

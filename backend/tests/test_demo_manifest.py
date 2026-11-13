"""B2 Demo baseline: the manifest file matches C1's frozen table and the forecast table."""

import asyncio
import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.models.domain import InventoryItem, ShoppingItem, Transaction
from app.services.forecast import ForecastService, ForecastSettings

SCRIPT = Path(__file__).resolve().parents[2] / "data" / "build_demo_manifest.py"
spec = importlib.util.spec_from_file_location("build_demo_manifest", SCRIPT)
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)

# C1's frozen baseline, Issue #122 comment of 2026-11-12: id, name, quantity, unit, location.
FROZEN = {
    "seed-detergent": ("洗衣精", 1, "瓶", "陽台洗衣機旁"),
    "seed-tissue": ("衛生紙", 8, "包", "浴室櫃子"),
    "seed-flour": ("麵粉", 0, "包", "廚房食品櫃"),
    "seed-soy-sauce": ("醬油", 2, "瓶", "廚房調味架"),
    "seed-salt": ("鹽", 3, "包", "廚房調味架"),
    "seed-battery": ("電池", 4, "顆", "客廳抽屜"),
    "seed-sugar": ("糖", 2, "包", "廚房食品櫃"),
}


def manifest():
    return json.loads(demo.MANIFEST_PATH.read_text(encoding="utf-8"))


def test_manifest_file_is_up_to_date():
    assert demo.render_manifest(demo.build_manifest()) == demo.MANIFEST_PATH.read_text(
        encoding="utf-8"
    )


def test_inventory_matches_the_frozen_baseline():
    items = [InventoryItem.model_validate(row) for row in manifest()["inventory"]]

    assert {i.item_id: (i.name, i.quantity, i.unit, i.location) for i in items} == FROZEN


def test_shopping_has_only_the_two_pending_rows_linked_to_inventory():
    rows = [ShoppingItem.model_validate(row) for row in manifest()["shopping"]]

    assert {r.item_id: (r.quantity, r.unit, r.status.value) for r in rows} == {
        "seed-detergent": (2, "瓶", "pending"),
        "seed-flour": (1, "包", "pending"),
    }
    assert all(r.item_id in FROZEN for r in rows)


def test_every_record_is_the_demo_household_and_ids_are_unique():
    data = manifest()
    transactions = [Transaction.model_validate(row) for row in data["transactions"]]

    assert data["household_id"] == "demo-household"
    assert all(
        row["household_id"] == "demo-household"
        for key in ("inventory", "shopping", "transactions")
        for row in data[key]
    )
    assert len({t.transaction_id for t in transactions}) == len(transactions)
    assert len({t.operation_id for t in transactions}) == len(transactions)
    assert {t.item_id for t in transactions} <= set(FROZEN)
    assert all(t.reason.value == "purchase" and t.quantity_delta > 0 for t in transactions)


def test_event_time_is_taipei_noon_and_never_after_the_loading_date():
    transactions = [Transaction.model_validate(row) for row in manifest()["transactions"]]

    assert all(t.created_at.hour == 4 and t.created_at.tzinfo is not None for t in transactions)
    assert max(t.created_at for t in transactions) <= demo.LOADED_AT


def test_expected_forecast_table_is_the_agreed_demo_story():
    table = demo.expected_forecasts()

    assert table["seed-detergent"]["days_until_empty"] == pytest.approx(3.0)
    assert table["seed-detergent"]["suggested_action"] == "consider_restock"
    assert table["seed-tissue"]["days_until_empty"] == pytest.approx(40.0)
    assert table["seed-tissue"]["suggested_action"] == "none"
    assert table["seed-flour"]["days_until_empty"] == pytest.approx(0.0)
    assert table["seed-flour"]["suggested_action"] == "consider_restock"
    assert table["seed-soy-sauce"]["days_until_empty"] == pytest.approx(50.0)
    for key in ("seed-salt", "seed-battery", "seed-sugar"):
        assert table[key]["status"] == "insufficient_data"
        assert table[key]["days_until_empty"] is None


def test_expected_table_does_not_depend_on_the_day_it_is_asked():
    baseline = demo.expected_forecasts()
    last = demo.valid_until() - timedelta(minutes=1)

    for now in (
        demo.LOADED_AT,
        datetime(2026, 11, 18, tzinfo=UTC),
        datetime(2026, 12, 31, tzinfo=UTC),
        last,
    ):
        assert demo.expected_forecasts(now) == baseline


def test_forecast_never_changes_the_confirmed_quantity():
    for item_id, row in demo.expected_forecasts().items():
        assert row["confirmed_quantity"] == FROZEN[item_id][1]


def test_service_lists_flour_then_detergent_and_not_tissue():
    inventory, _, transactions = demo.build_records()

    class Inventory:
        async def list_items(self, household_id):
            return inventory

    class History:
        async def list_transactions(self, household_id):
            return transactions

    service = ForecastService(
        Inventory(), History(), ForecastSettings(time_semantics_confirmed=True)
    )
    summary = asyncio.run(
        service.reorder_candidates("demo-household", demo.BASELINE_NOW.astimezone(UTC))
    )

    assert [f.item_id for f in summary.candidates] == ["seed-flour", "seed-detergent"]
    assert summary.estimated_count == 4
    assert summary.insufficient_data_count == 3

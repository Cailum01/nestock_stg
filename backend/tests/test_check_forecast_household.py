"""Unit tests for deploy/check_forecast_household.py using in-memory fakes (no Cosmos)."""

import asyncio
import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.models.domain import InventoryItem, Transaction, TransactionReason

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
spec = importlib.util.spec_from_file_location(
    "check_forecast_household", DEPLOY / "check_forecast_household.py"
)
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)

HOUSEHOLD = "test-household"
NOW = datetime.now(UTC)


def item(item_id="detergent", quantity=3, household_id=HOUSEHOLD):
    return InventoryItem(
        item_id=item_id,
        household_id=household_id,
        name="洗衣精",
        quantity=quantity,
        unit="瓶",
        created_at=NOW - timedelta(days=30),
        updated_at=NOW - timedelta(days=1),
    )


def purchase(transaction_id="tx-1", item_id="detergent", household_id=HOUSEHOLD):
    return Transaction(
        transaction_id=transaction_id,
        household_id=household_id,
        item_id=item_id,
        operation_id=f"op-{transaction_id}",
        quantity_delta=2,
        reason=TransactionReason.PURCHASE,
        created_at=NOW - timedelta(days=1),
    )


class FakeInventory:
    def __init__(self, items, drift=False):
        self.items = items
        self._drift = drift
        self.reads = 0

    async def list_items(self, household_id):
        self.reads += 1
        items = [i for i in self.items if i.household_id == household_id]
        # Simulates something changing stock while forecasting: second read differs.
        if self._drift and self.reads > 1:
            return [i.model_copy(update={"quantity": i.quantity + 1}) for i in items]
        return items


class FakeRepository:
    def __init__(self, transactions):
        self.transactions = transactions

    async def list_transactions(self, query):
        return [t for t in self.transactions if t.household_id == query.household_id]


def run(monkeypatch, inventory, repository, transaction_id=None, household_id=HOUSEHOLD):
    monkeypatch.setattr(
        check, "_create_cosmos_composition", lambda: (inventory, None, repository, None, None)
    )
    return asyncio.run(check.run(household_id, transaction_id))


def test_passes_when_purchase_is_counted_and_stock_untouched(monkeypatch, capsys):
    ok = run(monkeypatch, FakeInventory([item()]), FakeRepository([purchase()]), "tx-1")

    assert ok is True
    out = capsys.readouterr().out
    assert "without 0 -> with 1" in out
    assert "RESULT: PASS" in out


def test_passes_without_purchase_id_for_stock_check_only(monkeypatch):
    assert run(monkeypatch, FakeInventory([item()]), FakeRepository([])) is True


def test_fails_when_stock_changes_while_forecasting(monkeypatch, capsys):
    ok = run(monkeypatch, FakeInventory([item()], drift=True), FakeRepository([purchase()]))

    assert ok is False
    assert "stored quantities changed" in capsys.readouterr().out


def test_fails_when_purchase_transaction_not_found(monkeypatch, capsys):
    ok = run(monkeypatch, FakeInventory([item()]), FakeRepository([purchase()]), "missing")

    assert ok is False
    assert "transaction not found" in capsys.readouterr().out


def test_fails_when_household_has_no_inventory(monkeypatch, capsys):
    ok = run(monkeypatch, FakeInventory([item(household_id="other")]), FakeRepository([]))

    assert ok is False
    assert "no inventory found" in capsys.readouterr().out


def test_refuses_to_run_without_cosmos(monkeypatch, capsys):
    monkeypatch.setattr(
        check, "_create_cosmos_composition", lambda: (FakeInventory([]), None, None, None, None)
    )

    assert asyncio.run(check.run(HOUSEHOLD, None)) is False
    assert "Cosmos is not configured" in capsys.readouterr().out


def test_ignores_other_households_transactions(monkeypatch, capsys):
    other = purchase(transaction_id="tx-other", household_id="other")
    ok = run(monkeypatch, FakeInventory([item()]), FakeRepository([other]), "tx-other")

    assert ok is False
    assert "transaction not found" in capsys.readouterr().out

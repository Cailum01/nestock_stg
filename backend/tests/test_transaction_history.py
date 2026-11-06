import asyncio
from datetime import UTC, datetime, timedelta

from app.core.config import settings
from app.models.domain import TransactionReason
from app.services.demo import DemoInventoryService
from app.services.interfaces import TransactionHistory
from app.services.transaction_history import DemoTransactionHistory

NOW = datetime(2026, 11, 2, tzinfo=UTC)


def history() -> DemoTransactionHistory:
    return DemoTransactionHistory(clock=lambda: NOW)


def test_demo_history_conforms_to_port():
    service: TransactionHistory = history()

    assert asyncio.run(service.list_transactions(settings.demo_household_id))


def test_demo_history_is_household_scoped():
    assert asyncio.run(history().list_transactions("other-household")) == []


def test_demo_history_is_newest_first_and_relative_to_clock():
    records = asyncio.run(history().list_transactions(settings.demo_household_id))

    assert [r.created_at for r in records] == [
        NOW - timedelta(days=30),
        NOW - timedelta(days=75),
        NOW - timedelta(days=120),
    ]
    assert all(r.reason == TransactionReason.PURCHASE and r.quantity_delta > 0 for r in records)


def test_demo_history_has_stable_ids():
    first = asyncio.run(history().list_transactions(settings.demo_household_id))
    later = DemoTransactionHistory(clock=lambda: NOW + timedelta(days=1))
    second = asyncio.run(later.list_transactions(settings.demo_household_id))

    assert [r.transaction_id for r in first] == [r.transaction_id for r in second]
    assert len({r.operation_id for r in first}) == len(first)


def test_demo_history_refers_to_demo_inventory_items():
    items = asyncio.run(DemoInventoryService().list_items(settings.demo_household_id))
    records = asyncio.run(history().list_transactions(settings.demo_household_id))

    assert {r.item_id for r in records} <= {i.item_id for i in items}

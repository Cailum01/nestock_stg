import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.models.domain import InventoryItem, Transaction, TransactionReason
from app.models.forecast import ForecastMethod, ForecastStatus, ItemForecast, SuggestedAction
from app.services.forecast import (
    ForecastService,
    ForecastSettings,
    ItemNotFoundError,
    estimate,
)

NOW = datetime(2026, 3, 1, tzinfo=UTC)


def item(quantity=10.0, item_id="soy-sauce", household_id="demo"):
    return InventoryItem(
        item_id=item_id,
        household_id=household_id,
        name="醬油",
        quantity=quantity,
        unit="瓶",
        created_at=NOW - timedelta(days=200),
        updated_at=NOW - timedelta(days=1),
    )


def tx(days_ago, delta, reason=TransactionReason.CONSUMPTION, item_id="soy-sauce", hh="demo"):
    return Transaction(
        transaction_id=f"tx-{item_id}-{days_ago}-{delta}",
        household_id=hh,
        item_id=item_id,
        operation_id=f"op-{days_ago}",
        quantity_delta=delta,
        reason=reason,
        created_at=NOW - timedelta(days=days_ago),
    )


def test_estimates_daily_rate_and_days_until_empty():
    result = estimate(item(quantity=10), [tx(10, -2), tx(5, -3)], NOW)

    assert result.status == ForecastStatus.ESTIMATED
    assert result.daily_consumption == pytest.approx(0.5)
    assert result.days_until_empty == pytest.approx(20)
    assert result.estimated_empty_at == NOW + timedelta(days=20)
    assert result.suggested_action == SuggestedAction.NONE
    assert result.consumption_event_count == 2
    assert result.method == ForecastMethod.CONSUMPTION


def test_suggests_restock_within_reorder_days():
    result = estimate(item(quantity=2), [tx(10, -2), tx(5, -3)], NOW)

    assert result.days_until_empty == pytest.approx(4)
    assert result.suggested_action == SuggestedAction.CONSIDER_RESTOCK


def test_one_purchase_and_no_consumption_is_insufficient_data():
    result = estimate(item(), [tx(3, 1, reason=TransactionReason.PURCHASE)], NOW)

    assert result.status == ForecastStatus.INSUFFICIENT_DATA
    assert result.suggested_action == SuggestedAction.CONFIRM_QUANTITY
    assert result.daily_consumption is None
    assert result.days_until_empty is None
    assert result.estimated_empty_at is None
    assert result.method is None


def test_ignores_other_items_households_reasons_and_out_of_window_records():
    records = [
        tx(10, -2),
        tx(5, -3),
        tx(4, -50, item_id="rice"),
        tx(4, -50, hh="other"),
        tx(4, -50, reason=TransactionReason.ADJUSTMENT),
        tx(4, 6, reason=TransactionReason.PURCHASE),
        tx(120, -50),
        tx(-1, -50),
    ]

    result = estimate(item(quantity=10), records, NOW)

    assert result.consumption_event_count == 2
    assert result.daily_consumption == pytest.approx(0.5)


def test_settings_change_threshold_without_changing_contract():
    settings = ForecastSettings(consumption_window_days=7, reorder_days=30)

    result = estimate(item(), [tx(10, -2), tx(5, -3)], NOW, settings)

    assert result.status == ForecastStatus.INSUFFICIENT_DATA
    assert result.consumption_event_count == 1


def purchase(days_ago, delta):
    return tx(days_ago, delta, reason=TransactionReason.PURCHASE)


def test_falls_back_to_purchase_interval_without_consumption_records():
    # 2 + 2 bottles used between day 60 and day 0; the latest 2 are still on hand.
    result = estimate(item(quantity=2), [purchase(60, 2), purchase(30, 2), purchase(0, 2)], NOW)

    assert result.status == ForecastStatus.ESTIMATED
    assert result.method == ForecastMethod.PURCHASE_INTERVAL
    assert result.daily_consumption == pytest.approx(4 / 60)
    assert result.days_until_empty == pytest.approx(30)
    assert result.purchase_event_count == 3


def test_consumption_records_take_priority_over_purchases():
    records = [tx(10, -2), tx(5, -3), purchase(60, 2), purchase(30, 2)]

    result = estimate(item(quantity=10), records, NOW)

    assert result.method == ForecastMethod.CONSUMPTION
    assert result.daily_consumption == pytest.approx(0.5)


def test_one_consumption_record_still_uses_purchase_interval():
    result = estimate(item(quantity=2), [tx(3, -1), purchase(60, 2), purchase(0, 2)], NOW)

    assert result.method == ForecastMethod.PURCHASE_INTERVAL
    assert result.daily_consumption == pytest.approx(2 / 60)


def test_purchase_window_is_longer_than_consumption_window():
    result = estimate(item(), [purchase(170, 2), purchase(100, 2)], NOW)

    assert result.method == ForecastMethod.PURCHASE_INTERVAL
    assert estimate(item(), [purchase(200, 2), purchase(100, 2)], NOW).status == (
        ForecastStatus.INSUFFICIENT_DATA
    )


def test_same_day_purchases_are_insufficient_data():
    result = estimate(item(), [purchase(5, 2), purchase(5, 1)], NOW)

    assert result.status == ForecastStatus.INSUFFICIENT_DATA
    assert result.suggested_action == SuggestedAction.CONFIRM_QUANTITY


def test_zero_quantity_is_empty_now():
    result = estimate(item(quantity=0), [tx(10, -2), tx(5, -3)], NOW)

    assert result.days_until_empty == 0
    assert result.suggested_action == SuggestedAction.CONSIDER_RESTOCK


def test_forecast_never_changes_confirmed_quantity():
    source = item(quantity=3)

    result = estimate(source, [tx(10, -2), tx(5, -3)], NOW)

    assert source.quantity == 3
    assert result.confirmed_quantity == 3


def test_forecast_model_rejects_unagreed_fields():
    data = estimate(item(), [], NOW).model_dump()

    with pytest.raises(ValidationError):
        ItemForecast(**data, predicted_quantity=1)


class FakeInventory:
    def __init__(self, items):
        self.items = items

    async def list_items(self, household_id):
        return [i for i in self.items if i.household_id == household_id]


class FakeHistory:
    def __init__(self, records):
        self.records = records
        self.calls = []

    async def list_transactions(self, household_id):
        self.calls.append(household_id)
        return [r for r in self.records if r.household_id == household_id]


def test_service_reads_ports_and_estimates():
    history = FakeHistory([tx(10, -2), tx(5, -3)])
    service = ForecastService(FakeInventory([item(quantity=10)]), history)

    result = asyncio.run(service.forecast_item("demo", "soy-sauce", NOW))

    assert result.days_until_empty == pytest.approx(20)
    assert history.calls == ["demo"]


def test_service_rejects_unknown_or_other_household_item():
    service = ForecastService(FakeInventory([item(household_id="other")]), FakeHistory([]))

    with pytest.raises(ItemNotFoundError):
        asyncio.run(service.forecast_item("demo", "soy-sauce", NOW))

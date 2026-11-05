import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.models.domain import InventoryItem, Transaction, TransactionReason
from app.models.forecast import (
    ForecastMethod,
    ForecastStatus,
    ItemForecast,
    SufficiencyAnswer,
    SufficiencyCheck,
    SuggestedAction,
)
from app.services.forecast import (
    ForecastService,
    ForecastSettings,
    ItemNotFoundError,
    estimate,
)

NOW = datetime(2026, 3, 1, tzinfo=UTC)
# P1 extensions, off by default; tests of those paths opt in explicitly.
P1 = ForecastSettings(use_sufficiency_check=True, use_household_reference=True)


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
    assert result.suggested_action == SuggestedAction.NONE
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
    assert result.suggested_action == SuggestedAction.NONE


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


def test_service_forecasts_every_household_item_with_one_history_read():
    history = FakeHistory([tx(10, -2), tx(5, -3)])
    items = [item(quantity=10), item(item_id="rice"), item(item_id="x", household_id="other")]
    service = ForecastService(FakeInventory(items), history)

    results = asyncio.run(service.forecast_household("demo", NOW))

    assert [r.item_id for r in results] == ["soy-sauce", "rice"]
    assert results[1].status == ForecastStatus.INSUFFICIENT_DATA
    assert history.calls == ["demo"]


def named(item_id, name, unit, quantity=100.0):
    return InventoryItem(
        item_id=item_id,
        household_id="demo",
        name=name,
        quantity=quantity,
        unit=unit,
        created_at=NOW - timedelta(days=200),
        updated_at=NOW,
    )


def item_purchase(item_id, days_ago, delta):
    return tx(days_ago, delta, reason=TransactionReason.PURCHASE, item_id=item_id)


def test_household_factor_scales_reference_for_insufficient_same_unit_item():
    # Shampoo: 600 ml used over 60 days = 10 ml/day, twice the 5 ml reference.
    shampoo = named("shampoo", "洗髮精", "ml")
    body_wash = named("body-wash", "沐浴乳", "ml", quantity=240)
    records = [item_purchase("shampoo", 60, 600), item_purchase("shampoo", 0, 600)]
    service = ForecastService(FakeInventory([shampoo, body_wash]), FakeHistory(records), P1)

    results = asyncio.run(service.forecast_household("demo", NOW))

    assert results[0].method == ForecastMethod.PURCHASE_INTERVAL
    assert results[1].method == ForecastMethod.REFERENCE_SCALED
    assert results[1].household_factor == pytest.approx(2)
    assert results[1].daily_consumption == pytest.approx(8)
    assert results[1].days_until_empty == pytest.approx(30)


def test_reference_is_skipped_when_units_do_not_match():
    shampoo = named("shampoo", "洗髮精", "ml")
    bottled = named("body-wash", "沐浴乳", "瓶", quantity=1)
    records = [item_purchase("shampoo", 60, 600), item_purchase("shampoo", 0, 600)]
    service = ForecastService(FakeInventory([shampoo, bottled]), FakeHistory(records), P1)

    results = asyncio.run(service.forecast_household("demo", NOW))

    assert results[1].status == ForecastStatus.INSUFFICIENT_DATA
    assert results[1].suggested_action == SuggestedAction.CHECK_SUFFICIENCY
    assert results[1].household_factor is None


def test_no_factor_without_own_same_unit_records_means_card():
    service = ForecastService(
        FakeInventory([named("body-wash", "沐浴乳", "ml")]), FakeHistory([]), P1
    )

    result = asyncio.run(service.forecast_household("demo", NOW))[0]

    assert result.status == ForecastStatus.INSUFFICIENT_DATA
    assert result.suggested_action == SuggestedAction.CHECK_SUFFICIENCY


def check(days_ago, answer, item_id="soy-sauce"):
    return SufficiencyCheck(
        check_id=f"check-{item_id}-{days_ago}",
        household_id="demo",
        item_id=item_id,
        answer=answer,
        answered_at=NOW - timedelta(days=days_ago),
    )


def test_enough_answer_is_kept_and_pauses_the_card():
    answers = [check(3, SufficiencyAnswer.ENOUGH)]

    result = estimate(item(), [purchase(40, 2)], NOW, P1, checks=answers)

    assert result.status == ForecastStatus.INSUFFICIENT_DATA
    assert result.suggested_action == SuggestedAction.NONE
    later = estimate(item(), [purchase(40, 2)], NOW + timedelta(days=10), P1, checks=answers)
    assert later.suggested_action == SuggestedAction.CHECK_SUFFICIENCY


def test_not_enough_bounds_usage_days_and_suggests_restock():
    # Bought 2 on day 40; still enough on day 20 (20 days in); not enough on day 10 (30 days in).
    answers = [check(20, SufficiencyAnswer.ENOUGH), check(10, SufficiencyAnswer.NOT_ENOUGH)]

    result = estimate(item(quantity=1), [purchase(40, 2)], NOW, P1, checks=answers)

    assert result.method == ForecastMethod.SUFFICIENCY_CHECK
    assert result.suggested_action == SuggestedAction.CONSIDER_RESTOCK
    assert (result.usage_days_min, result.usage_days_max) == (20, 30)
    assert result.daily_consumption == pytest.approx(2 / 25)
    # Not restocked yet, so the next purchase is now.
    assert result.next_purchase_from == result.next_purchase_to == NOW
    assert result.confirmed_quantity == 1


def test_window_moves_to_the_restock_after_not_enough():
    answers = [check(10, SufficiencyAnswer.NOT_ENOUGH)]
    # Two purchases would normally use the purchase interval; require three here.
    settings = ForecastSettings(min_purchase_events=3, use_sufficiency_check=True)

    result = estimate(
        item(quantity=2), [purchase(40, 2), purchase(8, 2)], NOW, settings, checks=answers
    )

    assert result.method == ForecastMethod.SUFFICIENCY_CHECK
    assert (result.usage_days_min, result.usage_days_max) == (0, 30)
    assert result.next_purchase_from == NOW - timedelta(days=8)
    assert result.next_purchase_to == NOW - timedelta(days=8) + timedelta(days=30)
    assert result.days_until_empty == pytest.approx(2 / (2 / 15))


def test_own_records_win_over_answers():
    answers = [check(10, SufficiencyAnswer.NOT_ENOUGH)]

    result = estimate(item(quantity=10), [tx(10, -2), tx(5, -3)], NOW, P1, checks=answers)

    assert result.method == ForecastMethod.CONSUMPTION


class FakeChecks:
    def __init__(self, answers):
        self.answers = answers

    async def list_checks(self, household_id):
        return [a for a in self.answers if a.household_id == household_id]


def test_service_reads_answers_through_port():
    service = ForecastService(
        FakeInventory([item(quantity=1)]),
        FakeHistory([purchase(40, 2)]),
        P1,
        checks=FakeChecks([check(10, SufficiencyAnswer.NOT_ENOUGH)]),
    )

    result = asyncio.run(service.forecast_item("demo", "soy-sauce", NOW))

    assert result.method == ForecastMethod.SUFFICIENCY_CHECK


def test_p1_extensions_are_off_by_default():
    settings = ForecastSettings()

    assert not settings.use_sufficiency_check
    assert not settings.use_household_reference


def test_default_ignores_answers_and_shows_no_card():
    answers = [check(20, SufficiencyAnswer.ENOUGH), check(10, SufficiencyAnswer.NOT_ENOUGH)]

    result = estimate(item(quantity=1), [purchase(40, 2)], NOW, checks=answers)

    assert result.status == ForecastStatus.INSUFFICIENT_DATA
    assert result.suggested_action == SuggestedAction.NONE
    assert result.method is None
    assert result.usage_days_min is None


def test_default_does_not_scale_reference_for_insufficient_items():
    shampoo = named("shampoo", "洗髮精", "ml")
    body_wash = named("body-wash", "沐浴乳", "ml", quantity=240)
    records = [item_purchase("shampoo", 60, 600), item_purchase("shampoo", 0, 600)]
    service = ForecastService(FakeInventory([shampoo, body_wash]), FakeHistory(records))

    results = asyncio.run(service.forecast_household("demo", NOW))

    assert results[0].method == ForecastMethod.PURCHASE_INTERVAL
    assert results[1].status == ForecastStatus.INSUFFICIENT_DATA
    assert results[1].method is None
    assert results[1].household_factor is None
    assert results[1].daily_consumption is None


def test_quantity_sign_does_not_change_the_estimate():
    """Reason decides the kind; the sign of quantity_delta is never read (PR #16)."""
    buy = TransactionReason.PURCHASE
    signed = estimate(item(quantity=10), [tx(10, -2), tx(5, -3)], NOW)
    unsigned = estimate(item(quantity=10), [tx(10, 2), tx(5, 3)], NOW)
    assert unsigned.daily_consumption == pytest.approx(signed.daily_consumption)
    assert unsigned.consumption_event_count == signed.consumption_event_count == 2

    interval_signed = estimate(item(), [tx(30, 6, buy), tx(0, 6, buy)], NOW)
    interval_flipped = estimate(item(), [tx(30, -6, buy), tx(0, -6, buy)], NOW)
    assert interval_flipped.method == ForecastMethod.PURCHASE_INTERVAL
    assert interval_flipped.daily_consumption == pytest.approx(interval_signed.daily_consumption)

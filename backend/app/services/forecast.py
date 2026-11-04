"""B2 read-only consumption forecast. No inventory write port, no persistence.

Baseline: average daily use, from recorded consumption when there is enough of
it, otherwise from the interval between purchases, applied to the confirmed
quantity.

Portability: `estimate` reads only Transaction columns (household_id, item_id,
reason, quantity_delta, created_at) and one confirmed quantity, and uses only
filter/sum/min/max, so the same rule can run as a grouped SQL query over a lake
table. Storage stays behind `TransactionHistory`; a lake or warehouse source is
another adapter, and its engine and table format are B1/team decisions.

When records are insufficient the default result is `insufficient_data` with no
guess (v8 §3.4). Two P1 extensions are kept behind `ForecastSettings` switches,
off by default per A1's review of PR #16: the user's "still enough?" answers and
a CHECK_SUFFICIENCY card (`use_sufficiency_check`, waiting for the card decision),
then a household factor (`use_household_reference`, see forecast_reference).
The household's own information always takes priority, no head count is inferred,
and the user is never asked about household size.

Open gaps:
- Feature-only Transactions and links wait for D-6; only `item_id` is read.
- There is no quantity-confirmation timestamp yet, so the estimate starts from
  `now`, not from when the quantity was last confirmed.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median
from typing import Protocol

from app.models.domain import InventoryItem, Transaction, TransactionReason
from app.models.forecast import (
    ForecastMethod,
    ForecastStatus,
    ItemForecast,
    SufficiencyAnswer,
    SufficiencyCheck,
    SuggestedAction,
)
from app.services.forecast_reference import match_reference
from app.services.interfaces import InventoryService, TransactionHistory

SECONDS_PER_DAY = 86_400


@dataclass(frozen=True)
class ForecastSettings:
    """Pilot defaults; B2 tunes these without changing the result contract."""

    consumption_window_days: int = 90
    purchase_window_days: int = 180
    min_consumption_events: int = 2
    min_purchase_events: int = 2
    reorder_days: float = 7.0
    # After an "enough" answer, do not show the card again for this many days.
    check_cooldown_days: float = 7.0
    # P1, off by default: v8 §3.4 returns insufficient_data without guessing.
    use_sufficiency_check: bool = False
    use_household_reference: bool = False


class SufficiencyCheckHistory(Protocol):
    """Read port for card answers; storage and writes need B1/A1 agreement."""

    async def list_checks(self, household_id: str) -> list[SufficiencyCheck]: ...


class ItemNotFoundError(LookupError):
    """The item is not in the household's active inventory."""


def _events(
    item: InventoryItem,
    transactions: list[Transaction],
    reason: TransactionReason,
    window_days: int,
    now: datetime,
) -> list[Transaction]:
    window_start = now - timedelta(days=window_days)
    return [
        tx
        for tx in transactions
        if tx.household_id == item.household_id
        and tx.item_id == item.item_id
        and tx.reason == reason
        and (
            tx.quantity_delta > 0 if reason == TransactionReason.PURCHASE else tx.quantity_delta < 0
        )
        and window_start <= tx.created_at <= now
    ]


def _days(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / SECONDS_PER_DAY


def _consumption_rate(consumed: list[Transaction], now: datetime) -> float | None:
    """Recorded consumption since the earliest consumption in the window."""
    if not consumed:
        return None
    span = _days(min(tx.created_at for tx in consumed), now)
    return sum(-tx.quantity_delta for tx in consumed) / span if span > 0 else None


def _purchase_interval_rate(purchases: list[Transaction]) -> float | None:
    """Assumes each restock replaces what was used: everything bought before the
    latest purchase was consumed between the first and the latest purchase."""
    ordered = sorted(purchases, key=lambda tx: tx.created_at)
    if len(ordered) < 2:
        return None
    span = _days(ordered[0].created_at, ordered[-1].created_at)
    return sum(tx.quantity_delta for tx in ordered[:-1]) / span if span > 0 else None


def estimate(
    item: InventoryItem,
    transactions: list[Transaction],
    now: datetime,
    settings: ForecastSettings = ForecastSettings(),
    checks: list[SufficiencyCheck] | None = None,
) -> ItemForecast:
    """Prefer recorded consumption; fall back to purchase intervals, which users log
    far more often, then (P1) to "still enough?" answers; otherwise insufficient data."""
    consumed = _events(
        item, transactions, TransactionReason.CONSUMPTION, settings.consumption_window_days, now
    )
    purchases = _events(
        item, transactions, TransactionReason.PURCHASE, settings.purchase_window_days, now
    )
    base = dict(
        household_id=item.household_id,
        item_id=item.item_id,
        confirmed_quantity=item.quantity,
        unit=item.unit,
        generated_at=now,
        consumption_event_count=len(consumed),
        purchase_event_count=len(purchases),
    )

    method, daily = None, None
    if len(consumed) >= settings.min_consumption_events:
        method, daily = ForecastMethod.CONSUMPTION, _consumption_rate(consumed, now)
    if daily is None and len(purchases) >= settings.min_purchase_events:
        method, daily = ForecastMethod.PURCHASE_INTERVAL, _purchase_interval_rate(purchases)
    insufficient = ItemForecast(
        **base,
        status=ForecastStatus.INSUFFICIENT_DATA,
        suggested_action=(
            SuggestedAction.CHECK_SUFFICIENCY
            if settings.use_sufficiency_check
            else SuggestedAction.NONE
        ),
    )
    if daily is not None:
        return _with_rate(insufficient, method, daily, settings)
    if not settings.use_sufficiency_check:
        return insufficient

    answers = sorted(
        (
            c
            for c in checks or []
            if c.household_id == item.household_id
            and c.item_id == item.item_id
            and c.answered_at <= now
        ),
        key=lambda c: c.answered_at,
    )
    from_answers = _from_sufficiency(insufficient, purchases, answers, settings)
    if from_answers is not None:
        return from_answers
    if (
        answers
        and answers[-1].answer == SufficiencyAnswer.ENOUGH
        and _days(answers[-1].answered_at, now) < settings.check_cooldown_days
    ):
        return insufficient.model_copy(update=dict(suggested_action=SuggestedAction.NONE))
    return insufficient


def _from_sufficiency(
    forecast: ItemForecast,
    purchases: list[Transaction],
    answers: list[SufficiencyCheck],
    settings: ForecastSettings,
) -> ItemForecast | None:
    """Bound how long the last purchase lasted from the user's answers.

    The purchase lasted at least until the last "enough" after it, and at most until
    the latest "not enough". Not restocked yet: the next purchase is now. Restocked:
    the next purchase is likely that many days after the restock.
    """
    not_enough = [a for a in answers if a.answer == SufficiencyAnswer.NOT_ENOUGH]
    if not not_enough:
        return None
    ran_low = not_enough[-1].answered_at
    ordered = sorted(purchases, key=lambda tx: tx.created_at)
    before = [tx for tx in ordered if tx.created_at < ran_low]
    if not before:
        return None
    bought = before[-1]
    usage_max = _days(bought.created_at, ran_low)
    still_enough = [
        a.answered_at
        for a in answers
        if a.answer == SufficiencyAnswer.ENOUGH and bought.created_at < a.answered_at < ran_low
    ]
    usage_min = _days(bought.created_at, still_enough[-1]) if still_enough else 0.0
    daily = bought.quantity_delta / ((usage_min + usage_max) / 2)

    usage = dict(usage_days_min=usage_min, usage_days_max=usage_max)
    restocked = [tx for tx in ordered if tx.created_at >= ran_low]
    if restocked:
        restock = restocked[-1].created_at
        estimated = _with_rate(forecast, ForecastMethod.SUFFICIENCY_CHECK, daily, settings)
        return estimated.model_copy(
            update=dict(
                **usage,
                next_purchase_from=restock + timedelta(days=usage_min),
                next_purchase_to=restock + timedelta(days=usage_max),
            )
        )
    # Ran low and not restocked yet: the next purchase is now; quantity stays as confirmed.
    now = forecast.generated_at
    return forecast.model_copy(
        update=dict(
            **usage,
            status=ForecastStatus.ESTIMATED,
            method=ForecastMethod.SUFFICIENCY_CHECK,
            suggested_action=SuggestedAction.CONSIDER_RESTOCK,
            daily_consumption=daily,
            next_purchase_from=now,
            next_purchase_to=now,
        )
    )


def _with_rate(
    forecast: ItemForecast, method: ForecastMethod, daily: float, settings: ForecastSettings
) -> ItemForecast:
    days_left = max(forecast.confirmed_quantity, 0) / daily
    return forecast.model_copy(
        update=dict(
            status=ForecastStatus.ESTIMATED,
            method=method,
            suggested_action=(
                SuggestedAction.CONSIDER_RESTOCK
                if days_left <= settings.reorder_days
                else SuggestedAction.NONE
            ),
            daily_consumption=daily,
            days_until_empty=days_left,
            estimated_empty_at=forecast.generated_at + timedelta(days=days_left),
        )
    )


def household_factor(items: list[InventoryItem], forecasts: list[ItemForecast]) -> float | None:
    """How this household's recorded use compares with reference rates.

    Median ratio over items that have their own estimate and a same-unit reference.
    It describes usage only; it is not, and is never converted to, a head count.
    """
    ratios = [
        forecast.daily_consumption / reference.daily_per_person
        for item, forecast in zip(items, forecasts, strict=True)
        if forecast.status == ForecastStatus.ESTIMATED
        and forecast.method != ForecastMethod.REFERENCE_SCALED
        and forecast.daily_consumption is not None
        and (reference := match_reference(item)) is not None
    ]
    return median(ratios) if ratios else None


def apply_household_reference(
    item: InventoryItem,
    forecast: ItemForecast,
    factor: float | None,
    settings: ForecastSettings = ForecastSettings(),
) -> ItemForecast:
    """Own records always win; the reference fills in only insufficient items (P1)."""
    if (
        not settings.use_household_reference
        or forecast.status != ForecastStatus.INSUFFICIENT_DATA
        or factor is None
    ):
        return forecast
    reference = match_reference(item)
    if reference is None:
        return forecast
    scaled = _with_rate(
        forecast, ForecastMethod.REFERENCE_SCALED, reference.daily_per_person * factor, settings
    )
    return scaled.model_copy(update=dict(household_factor=factor))


class ForecastService:
    """Reads through the same ports as API routes; Agent tools should reuse it."""

    def __init__(
        self,
        inventory: InventoryService,
        history: TransactionHistory,
        settings: ForecastSettings = ForecastSettings(),
        checks: SufficiencyCheckHistory | None = None,
    ) -> None:
        self._inventory = inventory
        self._history = history
        self._settings = settings
        self._checks = checks

    async def forecast_item(self, household_id: str, item_id: str, now: datetime) -> ItemForecast:
        # The household factor needs every item, so one item reuses the household pass.
        forecasts = await self.forecast_household(household_id, now)
        found = next((f for f in forecasts if f.item_id == item_id), None)
        if found is None:
            raise ItemNotFoundError(item_id)
        return found

    async def forecast_household(self, household_id: str, now: datetime) -> list[ItemForecast]:
        """One history read for every active item, in inventory order."""
        items = await self._inventory.list_items(household_id)
        transactions = await self._history.list_transactions(household_id)
        answers = await self._checks.list_checks(household_id) if self._checks else []
        own = [estimate(item, transactions, now, self._settings, answers) for item in items]
        factor = household_factor(items, own)
        return [
            apply_household_reference(item, forecast, factor, self._settings)
            for item, forecast in zip(items, own, strict=True)
        ]

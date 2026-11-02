"""B2 read-only consumption forecast. No inventory write port, no persistence.

Baseline: average daily use, from recorded consumption when there is enough of
it, otherwise from the interval between purchases, applied to the confirmed
quantity.

Portability: `estimate` reads only Transaction columns (household_id, item_id,
reason, quantity_delta, created_at) and one confirmed quantity, and uses only
filter/sum/min/max, so the same rule can run as a grouped SQL query over a lake
table. Storage stays behind `TransactionHistory`; a lake or warehouse source is
another adapter, and its engine and table format are B1/team decisions.

Open gaps:
- Feature-only Transactions and links wait for D-6; only `item_id` is read.
- There is no quantity-confirmation timestamp yet, so the estimate starts from
  `now`, not from when the quantity was last confirmed.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.models.domain import InventoryItem, Transaction, TransactionReason
from app.models.forecast import ForecastMethod, ForecastStatus, ItemForecast, SuggestedAction
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
) -> ItemForecast:
    """Prefer recorded consumption; fall back to purchase intervals, which users log
    far more often; otherwise report insufficient data."""
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
    if daily is None:
        return ItemForecast(
            **base,
            status=ForecastStatus.INSUFFICIENT_DATA,
            suggested_action=SuggestedAction.CONFIRM_QUANTITY,
        )

    days_left = max(item.quantity, 0) / daily
    return ItemForecast(
        **base,
        status=ForecastStatus.ESTIMATED,
        method=method,
        suggested_action=(
            SuggestedAction.CONSIDER_RESTOCK
            if days_left <= settings.reorder_days
            else SuggestedAction.NONE
        ),
        daily_consumption=daily,
        days_until_empty=days_left,
        estimated_empty_at=now + timedelta(days=days_left),
    )


class ForecastService:
    """Reads through the same ports as API routes; Agent tools should reuse it."""

    def __init__(
        self,
        inventory: InventoryService,
        history: TransactionHistory,
        settings: ForecastSettings = ForecastSettings(),
    ) -> None:
        self._inventory = inventory
        self._history = history
        self._settings = settings

    async def forecast_item(self, household_id: str, item_id: str, now: datetime) -> ItemForecast:
        items = await self._inventory.list_items(household_id)
        item = next((i for i in items if i.item_id == item_id), None)
        if item is None:
            raise ItemNotFoundError(item_id)
        transactions = await self._history.list_transactions(household_id)
        return estimate(item, transactions, now, self._settings)

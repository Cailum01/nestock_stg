"""B2 read-only demo TransactionHistory. No store, database client, or mutation.

Dates are relative to an injected clock so the purchase-interval forecast stays
inside its window whenever the demo runs. A storage adapter replaces this class
behind the same `TransactionHistory` port.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.core.config import settings
from app.models.domain import Transaction, TransactionReason

# (days ago, quantity delta) for demo-rice, matching DemoInventoryService's unit (包).
DEMO_RICE_PURCHASES = ((120, 2.0), (75, 2.0), (30, 2.0))


def _utc_now() -> datetime:
    return datetime.now(UTC)


class DemoTransactionHistory:
    def __init__(self, clock: Callable[[], datetime] = _utc_now) -> None:
        self._clock = clock

    async def list_transactions(self, household_id: str) -> list[Transaction]:
        if household_id != settings.demo_household_id:
            return []
        now = self._clock()
        records = [
            Transaction(
                transaction_id=f"demo-tx-rice-{days_ago}",
                household_id=household_id,
                item_id="demo-rice",
                operation_id=f"demo-op-rice-{days_ago}",
                quantity_delta=delta,
                reason=TransactionReason.PURCHASE,
                created_at=now - timedelta(days=days_ago),
            )
            for days_ago, delta in DEMO_RICE_PURCHASES
        ]
        # Same order the history queries agreed with B1: newest first, then transaction_id.
        records.sort(key=lambda tx: tx.transaction_id)
        records.sort(key=lambda tx: tx.created_at, reverse=True)
        return records

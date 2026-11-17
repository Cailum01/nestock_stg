"""B2 read-only check: does forecasting use a purchase, and leave stock untouched?

Issue #157 follow-up. `/forecast` always reads `demo-household`, so it cannot see the
isolated household A1 used for scenarios F/G/H. This script runs the same
`ForecastService` against any household straight from Cosmos instead.

It proves forecast LOGIC on real Cosmos data. It is NOT the public cloud `/forecast`
route, and not Android. Say so when reporting.

Read-only: it never writes to Cosmos and never touches `demo-household` unless asked.

Checks, per household:
  1 Inventory unchanged: every forecast `confirmed_quantity` equals the stored
    InventoryItem.quantity, and the stored quantities are identical before and
    after forecasting (prediction never overwrites stock).
  2 Purchase counted (only with --purchase-transaction-id): forecasting with that
    purchase has one more purchase event for its item than forecasting without it.

Usage (needs `az login` and backend/.env with NESTOCK_COSMOS_*):
  cd backend
  uv run python ../deploy/check_forecast_household.py \\
      --household-id a1-cloud-20261116114237 \\
      --purchase-transaction-id 8c59864c55434fba8eb8c506453aeb9f
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.core.dependencies import _create_cosmos_composition  # noqa: E402
from app.models.domain import Transaction  # noqa: E402
from app.services.cosmos_transaction_history import CosmosTransactionHistory  # noqa: E402
from app.services.forecast import ForecastService  # noqa: E402


class _WithoutTransaction:
    """History that hides one transaction, to measure its effect."""

    def __init__(self, inner: CosmosTransactionHistory, transaction_id: str) -> None:
        self._inner = inner
        self._transaction_id = transaction_id

    async def list_transactions(self, household_id: str) -> list[Transaction]:
        records = await self._inner.list_transactions(household_id)
        return [tx for tx in records if tx.transaction_id != self._transaction_id]


def _quantities(items) -> dict[str, float]:
    return {item.item_id: item.quantity for item in items}


async def run(household_id: str, purchase_transaction_id: str | None) -> bool:
    inventory, _, read_repository, _, _ = _create_cosmos_composition()
    if read_repository is None:
        print("FAIL: Cosmos is not configured (NESTOCK_COSMOS_* missing); refusing demo data.")
        return False
    history = CosmosTransactionHistory(read_repository)
    now = datetime.now(UTC)
    ok = True

    before = _quantities(await inventory.list_items(household_id))
    transactions = await history.list_transactions(household_id)
    print(f"household {household_id}: {len(before)} items, {len(transactions)} transactions")
    if not before:
        print("FAIL: no inventory found for this household.")
        return False

    forecasts = await ForecastService(inventory, history).forecast_household(household_id, now)
    after = _quantities(await inventory.list_items(household_id))

    print("\nCheck 1: forecast never changes stock")
    for f in forecasts:
        same = f.confirmed_quantity == before.get(f.item_id) == after.get(f.item_id)
        ok &= same
        print(
            f"  {'PASS' if same else 'FAIL'} {f.item_id}: stock {before.get(f.item_id)}, "
            f"forecast confirmed_quantity {f.confirmed_quantity}, status {f.status.value}, "
            f"purchases {f.purchase_event_count}"
        )
    if before != after:
        ok = False
        print("  FAIL: stored quantities changed while forecasting")

    if purchase_transaction_id:
        print(f"\nCheck 2: purchase {purchase_transaction_id} is counted")
        target = next(
            (tx for tx in transactions if tx.transaction_id == purchase_transaction_id), None
        )
        if target is None:
            print("  FAIL: transaction not found in this household.")
            return False
        print(
            f"  found: item {target.item_id}, {target.reason.value}, "
            f"delta {target.quantity_delta:+d}, at {target.created_at.isoformat()}"
        )
        without = await ForecastService(
            inventory, _WithoutTransaction(history, purchase_transaction_id)
        ).forecast_household(household_id, now)
        with_count = next(f for f in forecasts if f.item_id == target.item_id)
        without_count = next(f for f in without if f.item_id == target.item_id)
        counted = with_count.purchase_event_count == without_count.purchase_event_count + 1
        ok &= counted
        print(
            f"  {'PASS' if counted else 'FAIL'} purchase events: "
            f"without {without_count.purchase_event_count} -> with "
            f"{with_count.purchase_event_count}"
        )
        if with_count.status.value == "insufficient_data":
            print("  note: still insufficient_data (too few purchases for a day estimate).")

    print("\nRESULT:", "PASS" if ok else "FAIL")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--household-id", required=True)
    parser.add_argument("--purchase-transaction-id")
    args = parser.parse_args()
    return 0 if asyncio.run(run(args.household_id, args.purchase_transaction_id)) else 1


if __name__ == "__main__":
    sys.exit(main())

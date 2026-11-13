"""B2 DEMO baseline: build the manifest for B1's Demo loader and the expected forecasts.

Writes nothing to Cosmos. It only writes `demo_household_manifest.json`, which B1's
loader (`python -m app.services.demo_seed --manifest ...`, PR #131) reads.

Items, locations, quantities and pending shopping rows are C1's frozen baseline
(Issue #122, comment of 2026-11-12). Transaction history is B2's.

Time (Issue #113, decided 2026-11-13): `Transaction.created_at` is the event time.
Each seed purchase is stamped 12:00 Asia/Taipei on the day it happened.

Why every history is purchases, not consumption: the consumption rate runs from the
first consumption record up to the moment of the question, so the answer changes
every day. The purchase-interval rate (everything bought before the latest purchase,
divided by the days from the first to the latest purchase) does not depend on when
the question is asked. The expected results below therefore hold on any day from
the loading date until the earliest purchase leaves the 180-day window (see
`VALID_UNTIL`).

Usage:
  cd backend
  uv run python ../data/build_demo_manifest.py            # write the manifest + print the table
  uv run python ../data/build_demo_manifest.py --check    # fail if the file is out of date
"""

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.models.domain import (  # noqa: E402
    InventoryItem,
    ShoppingItem,
    ShoppingStatus,
    Transaction,
    TransactionReason,
)
from app.services.forecast import ForecastSettings, estimate  # noqa: E402

HOUSEHOLD_ID = "demo-household"
TAIPEI = ZoneInfo("Asia/Taipei")
MANIFEST_PATH = Path(__file__).resolve().parent / "demo_household_manifest.json"

# Reference time for the expected table: the morning of the Final Demo (11/18).
BASELINE_NOW = datetime(2026, 11, 18, 9, 0, tzinfo=TAIPEI)
# State of the data when it is loaded: inventory and shopping rows were created here.
LOADED_AT = datetime(2026, 11, 12, 12, 0, tzinfo=TAIPEI)


@dataclass(frozen=True)
class Spec:
    slug: str
    name: str
    quantity: int
    unit: str
    location: str
    shopping_quantity: int | None  # pending Shopping row, None = no row
    purchases: tuple[tuple[str, int], ...]  # (day, quantity)


SPECS = (
    Spec(
        "detergent",
        "洗衣精",
        1,
        "瓶",
        "陽台洗衣機旁",
        2,
        # 2 bottles every 6 days: 14 bottles over 42 days = 1/3 per day, 1 left = 3 days.
        tuple(
            (day, 2)
            for day in (
                "2026-10-01",
                "2026-10-07",
                "2026-10-13",
                "2026-10-19",
                "2026-10-25",
                "2026-10-31",
                "2026-11-06",
                "2026-11-12",
            )
        ),
    ),
    Spec(
        "tissue",
        "衛生紙",
        8,
        "包",
        "浴室櫃子",
        None,
        # 6 packs every 30 days: 12 packs over 60 days = 0.2 per day, 8 left = 40 days.
        (("2026-08-14", 6), ("2026-09-13", 6), ("2026-10-13", 6)),
    ),
    Spec(
        "flour",
        "麵粉",
        0,
        "包",
        "廚房食品櫃",
        1,
        # 1 bag every 14 days, none left: out now, restock.
        (("2026-09-16", 1), ("2026-09-30", 1), ("2026-10-14", 1), ("2026-10-28", 1)),
    ),
    Spec(
        "soy-sauce",
        "醬油",
        2,
        "瓶",
        "廚房調味架",
        None,
        # 1 bottle every 25 days: 2 over 50 days = 0.04 per day, 2 left = 50 days.
        (("2026-08-20", 1), ("2026-09-14", 1), ("2026-10-09", 1)),
    ),
    Spec("salt", "鹽", 3, "包", "廚房調味架", None, (("2026-10-20", 1),)),
    Spec("battery", "電池", 4, "顆", "客廳抽屜", None, (("2026-10-05", 4),)),
    Spec("sugar", "糖", 2, "包", "廚房食品櫃", None, (("2026-10-10", 1),)),
)

PURCHASE_WINDOW_DAYS = ForecastSettings().purchase_window_days


def _noon(day: str) -> datetime:
    parsed = date.fromisoformat(day)
    return datetime(parsed.year, parsed.month, parsed.day, 12, tzinfo=TAIPEI).astimezone(UTC)


def item_id(spec: Spec) -> str:
    return f"seed-{spec.slug}"


def build_records() -> tuple[list[InventoryItem], list[ShoppingItem], list[Transaction]]:
    stamp = LOADED_AT.astimezone(UTC)
    inventory = [
        InventoryItem(
            item_id=item_id(s),
            household_id=HOUSEHOLD_ID,
            name=s.name,
            quantity=s.quantity,
            unit=s.unit,
            location=s.location,
            created_at=stamp,
            updated_at=stamp,
        )
        for s in SPECS
    ]
    shopping = [
        ShoppingItem(
            shopping_item_id=f"seed-shopping-{s.slug}",
            household_id=HOUSEHOLD_ID,
            name=s.name,
            quantity=s.shopping_quantity,
            unit=s.unit,
            status=ShoppingStatus.PENDING,
            created_at=stamp,
            updated_at=stamp,
            item_id=item_id(s),
        )
        for s in SPECS
        if s.shopping_quantity is not None
    ]
    transactions = [
        Transaction(
            transaction_id=f"seed-tx-{s.slug}-{day}",
            household_id=HOUSEHOLD_ID,
            item_id=item_id(s),
            operation_id=f"seed-op-{s.slug}-{day}",
            quantity_delta=quantity,
            reason=TransactionReason.PURCHASE,
            created_at=_noon(day),
        )
        for s in SPECS
        for day, quantity in s.purchases
    ]
    return inventory, shopping, transactions


def build_manifest() -> dict:
    inventory, shopping, transactions = build_records()

    def dump(records):
        return [record.model_dump(mode="json", exclude_none=True) for record in records]

    return {
        "household_id": HOUSEHOLD_ID,
        "inventory": dump(inventory),
        "shopping": dump(shopping),
        "transactions": dump(transactions),
    }


def expected_forecasts(now: datetime = BASELINE_NOW) -> dict[str, dict]:
    inventory, _, transactions = build_records()
    settings = ForecastSettings(time_semantics_confirmed=True)
    result = {}
    for item in inventory:
        forecast = estimate(item, transactions, now.astimezone(UTC), settings)
        result[item.item_id] = {
            "name": item.name,
            "status": forecast.status.value,
            "method": forecast.method.value if forecast.method else None,
            "days_until_empty": forecast.days_until_empty,
            "suggested_action": forecast.suggested_action.value,
            "confirmed_quantity": forecast.confirmed_quantity,
        }
    return result


def valid_until() -> datetime:
    """Last moment every purchase is still inside the purchase window."""
    earliest = min(_noon(day) for s in SPECS for day, _ in s.purchases)
    return earliest + timedelta(days=PURCHASE_WINDOW_DAYS)


def render_manifest(manifest: dict) -> str:
    return json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"


def describe() -> str:
    lines = [
        f"reference time: {BASELINE_NOW.isoformat()}",
        f"stable until:   {valid_until().astimezone(TAIPEI).date()} "
        "(earliest purchase leaves the 180-day window)",
        "",
        "item_id            name    qty  method             days_left  action",
    ]
    for item_id_, row in expected_forecasts().items():
        days = "-" if row["days_until_empty"] is None else f"{row['days_until_empty']:.1f}"
        lines.append(
            f"{item_id_:<18} {row['name']:<6} {row['confirmed_quantity']:>3}  "
            f"{(row['method'] or row['status']):<17} {days:>9}  {row['suggested_action']}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true", help="fail if the manifest file differs")
    args = parser.parse_args(argv)

    text = render_manifest(build_manifest())
    if args.check:
        current = MANIFEST_PATH.read_text(encoding="utf-8") if MANIFEST_PATH.exists() else ""
        if current != text:
            print(f"{MANIFEST_PATH.name} is out of date; run without --check.", file=sys.stderr)
            return 1
    else:
        MANIFEST_PATH.write_text(text, encoding="utf-8")
        print(f"wrote {MANIFEST_PATH}")
    print(describe())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

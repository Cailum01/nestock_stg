"""A1 purchase-time rule (Issue #60): when a purchase happened, for Transaction.created_at.

The Agent turns what the user said ("昨天買的", "11/3") into a calendar day shown on the
approval card. Once the user confirms, this module turns that day into the stored time:

- no day, or today in Taipei -> the confirmation moment (`decided_at`)
- an earlier day             -> 12:00 Taipei time on that day
- a later day                -> rejected; a purchase cannot happen after it is confirmed

Before showing a card, pass the current time as `decided_at` to reject a future day early.
Taiwan has no daylight saving time, so Taipei is a fixed UTC+8 offset (no tzdata needed).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

TAIPEI = timezone(timedelta(hours=8), "Asia/Taipei")
PAST_DAY_TIME = time(12, 0)
"""A past day without a time is stored at noon Taipei, which is the same calendar day in UTC."""


class FuturePurchaseDate(ValueError):
    """The purchase day is after the confirmation day (Taipei)."""


def taipei_today(moment: datetime) -> date:
    """Calendar day of `moment` in Taipei."""
    _require_aware(moment)
    return moment.astimezone(TAIPEI).date()


def resolve_purchased_at(purchased_on: date | None, decided_at: datetime) -> datetime:
    """Purchase time for a card confirmed at `decided_at`; never later than `decided_at`."""
    if isinstance(purchased_on, datetime):
        raise TypeError("purchased_on is a calendar day, not a datetime.")
    today = taipei_today(decided_at)
    if purchased_on is None or purchased_on == today:
        return decided_at
    if purchased_on > today:
        raise FuturePurchaseDate(
            f"{purchased_on.isoformat()} is after {today.isoformat()} (Taipei)."
        )
    return datetime.combine(purchased_on, PAST_DAY_TIME, tzinfo=TAIPEI)


def _require_aware(moment: datetime) -> None:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("Times must be timezone-aware.")

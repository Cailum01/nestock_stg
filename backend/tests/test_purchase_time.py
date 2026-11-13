"""A1 purchase-time rule (Issue #60). Pure functions; no network."""

from datetime import date, datetime, timezone

import pytest

from app.services.purchase_time import (
    TAIPEI,
    FuturePurchaseDate,
    resolve_purchased_at,
    taipei_today,
)

CONFIRMED = datetime(2026, 11, 7, 10, 0, tzinfo=TAIPEI)


def test_no_day_is_the_confirmation_moment():
    assert resolve_purchased_at(None, CONFIRMED) == CONFIRMED


def test_today_is_the_confirmation_moment():
    assert resolve_purchased_at(date(2026, 11, 7), CONFIRMED) == CONFIRMED


@pytest.mark.parametrize("day", [date(2026, 11, 6), date(2026, 11, 3)])
def test_past_day_is_noon_taipei(day):
    result = resolve_purchased_at(day, CONFIRMED)

    assert result == datetime(day.year, day.month, day.day, 12, 0, tzinfo=TAIPEI)
    assert result.astimezone(timezone.utc).date() == day  # same calendar day in UTC
    assert result <= CONFIRMED


def test_future_day_is_rejected():
    with pytest.raises(FuturePurchaseDate):
        resolve_purchased_at(date(2026, 11, 8), CONFIRMED)


def test_today_follows_taipei_not_utc():
    confirmed = datetime(2026, 11, 6, 17, 0, tzinfo=timezone.utc)  # 11/7 01:00 in Taipei

    assert taipei_today(confirmed) == date(2026, 11, 7)
    assert resolve_purchased_at(date(2026, 11, 7), confirmed) == confirmed
    assert resolve_purchased_at(date(2026, 11, 6), confirmed) == datetime(
        2026, 11, 6, 12, 0, tzinfo=TAIPEI
    )


def test_times_without_timezone_are_refused():
    with pytest.raises(ValueError, match="timezone"):
        resolve_purchased_at(None, datetime(2026, 11, 7, 10, 0))


def test_a_datetime_is_not_a_day():
    with pytest.raises(TypeError):
        resolve_purchased_at(CONFIRMED, CONFIRMED)

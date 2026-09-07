"""When the NAV series is allowed to begin.

It used to start only once every holding had reported a close. A stock that
listed part-way through the window has none before it existed, so one small
late listing pushed the start forward to its listing date — SPCX listed
2026-06-12 and worth a few hundred dollars blanked both YTD and 1Y on the
Holdings tab, and left MAX measuring three months of a ten-year book.
"""

from __future__ import annotations

import pytest
from backend.analytics import _nav_series
from backend.models import Position

DAYS = [f"2026-0{month}-01" for month in range(1, 9)]     # Jan .. Aug


def pos(symbol, quantity, price):
    return Position(
        id=abs(hash(symbol)) % 10_000, symbol=symbol, name=symbol,
        quantity=quantity, cost_basis=0.0, price=price, broker="robinhood",
        asset_type="stock", currency="USD",
        # market_value is a stored field, not derived — a holding with no
        # price history is carried at exactly this.
        market_value=quantity * price,
    )


def closes(days, value):
    return {"dates": days, "closes": [value] * len(days)}


def test_a_small_late_listing_does_not_truncate_the_series():
    """The bug. A $500 holding must not decide where a $500,000 book's
    history begins."""
    positions = [pos("NVDA", 1000, 500.0), pos("SPCX", 10, 50.0)]
    history = {"NVDA": closes(DAYS, 500.0), "SPCX": closes(DAYS[6:], 50.0)}
    series = _nav_series(positions, history)
    assert [day for day, _ in series] == DAYS, "a late listing truncated the series"


def test_a_material_holding_is_still_waited_for():
    """The reason the rule existed. Summing whatever happened to be present
    ended the series with a cliff the size of the missing holding."""
    positions = [pos("NVDA", 1000, 500.0), pos("AAPL", 1000, 400.0)]
    history = {"NVDA": closes(DAYS, 500.0), "AAPL": closes(DAYS[4:], 400.0)}
    series = _nav_series(positions, history)
    assert [day for day, _ in series] == DAYS[4:]


def test_the_small_holding_is_carried_at_its_earliest_close_not_at_zero():
    """Carrying it flat bounds the error at its own size; dropping it would
    put a step in the line on the day it listed."""
    positions = [pos("NVDA", 1000, 500.0), pos("SPCX", 10, 50.0)]
    history = {"NVDA": closes(DAYS, 500.0), "SPCX": closes(DAYS[6:], 50.0)}
    values = dict(_nav_series(positions, history))
    assert values[DAYS[0]] == pytest.approx(500_000 + 500)
    assert values[DAYS[0]] == values[DAYS[7]], "the listing put a step in the line"


def test_a_book_where_everything_reports_is_unchanged():
    positions = [pos("NVDA", 10, 500.0), pos("AAPL", 10, 400.0)]
    history = {"NVDA": closes(DAYS, 500.0), "AAPL": closes(DAYS, 400.0)}
    assert len(_nav_series(positions, history)) == len(DAYS)


def test_a_holding_with_no_history_at_all_still_rides_flat():
    """Unchanged behaviour: it contributes its current market value so the
    series stays a meaningful aggregate rather than collapsing."""
    positions = [pos("NVDA", 10, 500.0), pos("FBALX", 100, 35.0)]
    history = {"NVDA": closes(DAYS, 500.0)}
    values = dict(_nav_series(positions, history))
    assert values[DAYS[0]] == pytest.approx(10 * 500.0 + 100 * 35.0)

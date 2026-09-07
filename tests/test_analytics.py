"""Tests for backend.analytics — period returns + NAV reconstruction.

The analytics module reconstructs a daily NAV series from today's basket × the
historical price series. These tests stub `fetch_price_history` so we don't
hit any provider.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from backend import analytics
from backend.models import Position


def _make_position(symbol, qty, price, asset_type="stock"):
    market_value = qty * price
    return Position(
        id=1,
        symbol=symbol,
        name=symbol,
        broker="manual",
        asset_type=asset_type,
        quantity=qty,
        average_cost=price * 0.9,
        current_price=price,
        sector="Technology",
        market_value=market_value,
        total_cost=qty * price * 0.9,
        unrealized_gain=market_value - qty * price * 0.9,
        unrealized_gain_pct=11.11,
    )


def _build_history(monkeypatch, history_payload):
    monkeypatch.setattr(
        analytics,
        "fetch_price_history",
        lambda period="3m", **_: {"period": period, "provider": "stub",
                                  "history": history_payload, "errors": []},
    )


def test_period_returns_with_single_holding(monkeypatch):
    today = datetime.now(UTC).date()
    dates = [(today - timedelta(days=offset)).isoformat() for offset in (400, 300, 200, 100, 30, 10, 0)]
    closes = [100.0, 110.0, 120.0, 130.0, 150.0, 160.0, 170.0]
    _build_history(monkeypatch, {"AAPL": {"dates": dates, "closes": closes}})

    positions = [_make_position("AAPL", qty=10, price=170.0)]
    result = analytics.period_returns(positions)

    assert result["indicative"] is True
    periods = {row["period"]: row for row in result["periods"]}
    assert "MAX" in periods
    # MAX return from 100 → 170 = +70%
    assert periods["MAX"]["return_pct"] == pytest.approx(70.0, rel=1e-3)
    # NAV series rebuilt with 10 shares
    nav = result["nav_series"]
    assert nav[0]["value"] == pytest.approx(10 * 100.0)
    assert nav[-1]["value"] == pytest.approx(10 * 170.0)


def test_period_returns_handles_no_history(monkeypatch):
    _build_history(monkeypatch, {})

    positions = [_make_position("AAPL", qty=10, price=170.0)]
    result = analytics.period_returns(positions)

    # No NAV series, no periods, but today's change still returns cleanly (0).
    assert result["nav_series"] == []
    assert result["periods"] == []
    assert result["today_change_pct"] == 0.0


def test_cash_carries_through_nav(monkeypatch):
    today = datetime.now(UTC).date()
    dates = [(today - timedelta(days=offset)).isoformat() for offset in (10, 0)]
    closes = [100.0, 110.0]
    _build_history(monkeypatch, {"AAPL": {"dates": dates, "closes": closes}})

    positions = [
        _make_position("AAPL", qty=10, price=110.0),
        _make_position("CASH", qty=1, price=500.0, asset_type="cash"),
    ]
    result = analytics.period_returns(positions)
    # First NAV point = 10×100 + 500 cash = 1500; last = 10×110 + 500 = 1600.
    nav = result["nav_series"]
    assert nav[0]["value"] == pytest.approx(1500.0)
    assert nav[-1]["value"] == pytest.approx(1600.0)


def test_period_bounds_includes_wtd_mtd_ytd():
    today = datetime.now(UTC).date()
    bounds = analytics._period_bounds(today)
    assert bounds["WTD"].weekday() == 0  # Monday
    assert bounds["MTD"].day == 1
    assert bounds["YTD"].month == 1 and bounds["YTD"].day == 1
    assert bounds["1Y"].year == today.year - 1


def test_nav_series_carries_ragged_tails_forward(monkeypatch):
    """A symbol whose provider is a day behind must not fall out of the NAV —
    the missing bar means "close not reported yet", never "sold". Without
    carry-forward the last day cratered by the whole holding and every period
    return measured against the cliff."""
    positions = [_make_position("AAA", 10, 100.0), _make_position("LAG", 10, 50.0)]
    series = analytics._nav_series(positions, {
        "AAA": {"dates": ["2026-08-12", "2026-08-13", "2026-08-14"], "closes": [100, 101, 102]},
        "LAG": {"dates": ["2026-08-12", "2026-08-13"], "closes": [50, 51]},
    })
    assert [day for day, _ in series] == ["2026-08-12", "2026-08-13", "2026-08-14"]
    # 08-14: AAA at 102, LAG carried at its last close 51 — not dropped to 0.
    assert series[-1][1] == 10 * 102 + 10 * 51


def test_today_change_uses_last_close_when_no_bar_landed_today(monkeypatch):
    """Until today's close lands, closes[-1] IS the previous close. Reaching
    for closes[-2] unconditionally measured today against the
    day-before-yesterday, overstating every move all session long."""
    position = _make_position("AAA", 10, 102.0)  # live price 102
    _build_history(monkeypatch, {
        "AAA": {"dates": ["2026-08-13", "2026-08-14"], "closes": [99.0, 100.0]},
    })
    absolute, pct = analytics._today_change([position])
    assert absolute == pytest.approx(10 * (102.0 - 100.0))
    assert pct == pytest.approx((20.0 / 1000.0) * 100)


def test_today_change_steps_back_one_bar_once_todays_close_lands(monkeypatch):
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    today = _dt.now(_UTC).date().isoformat()
    position = _make_position("AAA", 10, 102.0)
    _build_history(monkeypatch, {
        "AAA": {"dates": ["2026-08-14", today], "closes": [100.0, 102.0]},
    })
    absolute, pct = analytics._today_change([position])
    assert absolute == pytest.approx(10 * (102.0 - 100.0))
    assert pct == pytest.approx(2.0)


def test_the_indicative_note_names_the_cash_it_includes():
    """Two screens reported the same year as +3.50% and +2.26%, and neither
    said why: the X-ray benchmark measures the invested sleeve, these cards
    measure the whole portfolio with cash carried flat. On a book that is a
    third cash that is the entire gap, and an unexplained one reads as a bug."""
    from backend import analytics

    note = analytics.period_returns()["note"]
    assert "cash is included" in note.lower()
    assert "x-ray" in note.lower(), "the other figure is not named"

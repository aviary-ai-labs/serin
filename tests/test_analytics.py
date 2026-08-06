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
        lambda period="3m": {"period": period, "provider": "stub", "history": history_payload, "errors": []},
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

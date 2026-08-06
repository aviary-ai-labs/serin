"""Real TWR/MWR from the transactions log — hand-computable scenarios."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from backend import analytics, db
from backend.models import PositionIn, TransactionIn


def _days_ago(n: int) -> str:
    return (datetime.now(UTC) - timedelta(days=n)).date().isoformat()


def _fresh(tmp_path):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()


def test_unavailable_without_trades(tmp_path):
    _fresh(tmp_path)
    db.create_position(PositionIn(symbol="AAA", broker="manual", asset_type="stock", quantity=1, average_cost=100, current_price=100))
    result = analytics.transaction_returns(history={"AAA": {"dates": [_days_ago(2), _days_ago(1)], "closes": [100, 110]}})
    assert result["available"] is False


def test_twr_ignores_mid_period_deposit(tmp_path):
    """Buy 1 more share mid-window: price went +10% then +10%; TWR must be
    exactly 21% regardless of the deposit size."""
    _fresh(tmp_path)
    d0, d1, d2 = _days_ago(3), _days_ago(2), _days_ago(1)
    db.create_position(PositionIn(symbol="AAA", broker="manual", asset_type="stock", quantity=2, average_cost=100, current_price=121))
    db.create_transaction(TransactionIn(symbol="AAA", action="buy", quantity=1, price=110, occurred_at=d1))

    history = {"AAA": {"dates": [d0, d1, d2], "closes": [100.0, 110.0, 121.0]}}
    result = analytics.transaction_returns(history=history)

    assert result["available"] is True
    # Start: 1 share × $100. Day1: buy 1 @110 → V=220, flow=110 → r=10%.
    # Day2: V=242 → r=10%. TWR = 1.1 × 1.1 − 1 = 21%.
    assert result["start_value"] == 100.0
    assert result["end_value"] == 242.0
    assert result["twr_pct"] == pytest.approx(21.0, abs=1e-6)
    assert result["net_contributions"] == 110.0
    # Modified Dietz over the 2-day window: (242−100−110) / (100 + 110×0.5)
    assert result["mwr_period_pct"] == pytest.approx(32 / 155 * 100, abs=1e-3)
    # Sub-quarter windows are not annualized.
    assert result["mwr_annualized_pct"] is None


def test_dividend_credits_return_without_inflating_value(tmp_path):
    """$10 dividend on a $100 position that ends flat: TWR = +10%."""
    _fresh(tmp_path)
    d0, d1 = _days_ago(2), _days_ago(1)
    db.create_position(PositionIn(symbol="BBB", broker="manual", asset_type="stock", quantity=1, average_cost=100, current_price=100))
    db.create_transaction(TransactionIn(symbol="BBB", action="buy", quantity=1, price=100, occurred_at=d0))
    db.create_transaction(TransactionIn(symbol="BBB", action="dividend", quantity=0, price=10, occurred_at=d1))

    history = {"BBB": {"dates": [d0, d1], "closes": [100.0, 100.0]}}
    result = analytics.transaction_returns(history=history)

    assert result["available"] is True
    # Value flat at 100, dividend −10 flow → r = (100 + 10)/100 − 1 = 10%.
    assert result["twr_pct"] == pytest.approx(10.0, abs=1e-6)


def test_sell_is_a_withdrawal_not_a_loss(tmp_path):
    """Selling half the stake at an unchanged price must not read as a loss."""
    _fresh(tmp_path)
    d0, d1, d2 = _days_ago(3), _days_ago(2), _days_ago(1)
    db.create_position(PositionIn(symbol="CCC", broker="manual", asset_type="stock", quantity=1, average_cost=100, current_price=100))
    db.create_transaction(TransactionIn(symbol="CCC", action="buy", quantity=2, price=100, occurred_at=d0))
    db.create_transaction(TransactionIn(symbol="CCC", action="sell", quantity=1, price=100, occurred_at=d1))

    history = {"CCC": {"dates": [d0, d1, d2], "closes": [100.0, 100.0, 100.0]}}
    result = analytics.transaction_returns(history=history)

    assert result["available"] is True
    assert result["twr_pct"] == pytest.approx(0.0, abs=1e-6)
    assert result["end_value"] == 100.0


def test_xirr_solves_simple_annual_return():
    from datetime import date

    # Invest 100, get back 110 exactly one year later → 10% annualized.
    flows = [(date(2025, 1, 1), -100.0), (date(2026, 1, 1), 110.0)]
    rate = analytics._xirr(flows)
    assert rate == pytest.approx(0.10, abs=1e-3)


def test_period_returns_payload_includes_accurate_block(tmp_path, monkeypatch):
    _fresh(tmp_path)
    d0, d1 = _days_ago(2), _days_ago(1)
    db.create_position(PositionIn(symbol="AAA", broker="manual", asset_type="stock", quantity=1, average_cost=100, current_price=110))
    db.create_transaction(TransactionIn(symbol="AAA", action="buy", quantity=1, price=100, occurred_at=d0))
    monkeypatch.setattr(
        analytics,
        "fetch_price_history",
        lambda period="1y": {"history": {"AAA": {"dates": [d0, d1], "closes": [100.0, 110.0]}}},
    )

    payload = analytics.period_returns()

    assert payload["accurate"]["available"] is True
    assert payload["accurate"]["twr_pct"] == pytest.approx(10.0, abs=1e-6)

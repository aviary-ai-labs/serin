from __future__ import annotations

from backend import db
from backend.models import PositionIn
from backend.prices import fetch_price_history


def test_portfolio_summary_computes_gain_and_breakdowns(tmp_path):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()

    db.create_position(
        PositionIn(
            symbol="AAPL",
            name="Apple",
            broker="manual",
            asset_type="stock",
            quantity=10,
            average_cost=100,
            current_price=125,
            sector="Technology",
        )
    )
    db.create_position(
        PositionIn(
            symbol="CASH",
            name="Cash",
            broker="manual",
            asset_type="cash",
            quantity=1,
            average_cost=500,
            current_price=500,
            sector="",
        )
    )

    summary = db.portfolio_summary()

    assert summary.total_value == 1750
    assert summary.cash_value == 500
    assert summary.total_cost == 1000
    assert summary.total_gain == 250
    assert summary.total_gain_pct == 25
    assert summary.broker_breakdown["manual"] == 1750
    assert summary.sector_breakdown["Technology"] == 1250
    assert summary.sector_breakdown["Cash"] == 500
    assert summary.top_positions[0].symbol == "AAPL"


def test_price_history_returns_empty_state_without_positions(tmp_path):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()

    result = fetch_price_history()

    assert result["period"] == "3m"
    assert result["history"] == {}
    assert result["errors"] == []
    # Orchestrator now reports which provider it dispatched to so the UI can
    # surface a "served by Yahoo" or "served by FMP" hint.
    assert "provider" in result

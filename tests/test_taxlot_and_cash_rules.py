"""Items 3 and 5: cash positions without symbols, and lots that stay one holding.

Both were implemented before this brief landed; these tests pin the behaviour
the brief actually asks for, so it cannot quietly regress.
"""

from __future__ import annotations

import pytest
from backend import db, smart_import
from backend.models import PositionIn


@pytest.fixture
def store(tmp_path):
    db.set_db_path(tmp_path / "rules.db")
    db.init_db()
    return db


# --- item 5: a tax-lot view is one holding, not several -------------------

NFLX_LOT_VIEW = {
    "positions": [
        {"symbol": "NFLX", "name": "Netflix", "broker": "fidelity", "asset_type": "stock",
         "quantity": 650, "average_cost": 500.0, "current_price": 600.0,
         "tax_lots": [
             {"quantity": 300, "cost_basis": 450.0, "acquired_at": "2024-01-05"},
             {"quantity": 200, "cost_basis": 520.0, "acquired_at": "2024-06-11"},
             {"quantity": 150, "cost_basis": 560.0, "acquired_at": "2025-02-20"},
         ]},
    ]
}


def test_the_brief_s_nflx_example_becomes_one_holding_with_three_lots():
    rows = smart_import._normalize_positions(NFLX_LOT_VIEW)
    assert len(rows) == 1, "a tax-lot view must not become several NFLX positions"
    assert rows[0]["symbol"] == "NFLX"
    assert [lot["quantity"] for lot in rows[0]["tax_lots"]] == [300, 200, 150]


def test_lot_rows_returned_as_separate_positions_are_folded_together():
    """Models sometimes return one row per lot instead of an aggregate with a
    lots array. The result must still be a single holding."""
    per_lot = {
        "positions": [
            {"symbol": "NFLX", "broker": "fidelity", "asset_type": "stock",
             "quantity": 300, "average_cost": 450.0, "acquired_at": "2024-01-05"},
            {"symbol": "NFLX", "broker": "fidelity", "asset_type": "stock",
             "quantity": 200, "average_cost": 520.0, "acquired_at": "2024-06-11"},
            {"symbol": "NFLX", "broker": "fidelity", "asset_type": "stock",
             "quantity": 150, "average_cost": 560.0, "acquired_at": "2025-02-20"},
        ]
    }
    rows = smart_import._normalize_positions(per_lot)
    assert len(rows) == 1
    assert rows[0]["quantity"] == 650
    assert len(rows[0]["tax_lots"]) == 3


def test_the_same_symbol_at_two_brokers_stays_two_holdings():
    """Grouping is by symbol *and* broker. Merging across brokers would hide
    that the position is split, and break per-account reconciliation."""
    two_brokers = {
        "positions": [
            {"symbol": "NFLX", "broker": "fidelity", "asset_type": "stock",
             "quantity": 300, "average_cost": 450.0},
            {"symbol": "NFLX", "broker": "schwab", "asset_type": "stock",
             "quantity": 200, "average_cost": 520.0},
        ]
    }
    rows = smart_import._normalize_positions(two_brokers)
    assert len(rows) == 2
    assert {row["broker"] for row in rows} == {"fidelity", "schwab"}


def test_importing_a_lot_view_stores_the_lots_against_the_holding(store):
    rows = smart_import._normalize_positions(NFLX_LOT_VIEW)
    smart_import.bulk_insert(rows, replace=False)
    holdings = db.list_positions()
    assert [p.symbol for p in holdings] == ["NFLX"]
    lots = db.list_tax_lots(symbol="NFLX")
    assert sorted(lot.quantity for lot in lots) == [150, 200, 300]


def test_an_imported_lot_starts_fully_open(store):
    """Lots arrive from a *current* holdings view, so all of each remains.
    Any other default would import somebody's portfolio pre-sold."""
    smart_import.bulk_insert(smart_import._normalize_positions(NFLX_LOT_VIEW), replace=False)
    with db.connect() as conn:
        remaining = [
            row[0] for row in conn.execute(
                "SELECT remaining_quantity FROM tax_lots WHERE symbol='NFLX' ORDER BY quantity"
            )
        ]
    assert remaining == [150, 200, 300]


def test_a_holdings_view_never_invents_a_sale(store):
    """The brief is explicit: a current tax-lot screen shows only what is still
    owned, so it cannot establish a closed position."""
    rows = smart_import._normalize_positions(NFLX_LOT_VIEW)
    smart_import.bulk_insert(rows, replace=False)
    assert db.list_transactions() == [], "a holdings import fabricated transactions"


# --- item 3: cash needs no symbol ------------------------------------------


def test_a_cash_position_needs_no_symbol(store):
    """The form hides the symbol field for cash; the model has to agree, or
    the request the form sends is rejected."""
    cash = PositionIn(
        symbol="CASH", name="Schwab sweep", broker="schwab", asset_type="cash",
        quantity=5000.0, average_cost=1.0, current_price=1.0,
    )
    created = db.create_position(cash)
    assert created.asset_type == "cash"
    assert created.market_value == 5000.0


def test_cash_is_excluded_from_cost_and_gain(store):
    """Cash is a balance, not a bet. Counting it as invested capital would
    make holding cash look like a position that never moves."""
    db.create_position(PositionIn(symbol="AAPL", name="Apple", broker="fidelity",
                                  asset_type="stock", quantity=10, average_cost=100.0,
                                  current_price=150.0))
    db.create_position(PositionIn(symbol="CASH", name="Sweep", broker="fidelity",
                                  asset_type="cash", quantity=5000.0, average_cost=1.0,
                                  current_price=1.0))
    summary = db.portfolio_summary()
    assert summary.total_cost == 1000.0
    assert summary.total_gain == 500.0
    assert summary.cash_value == 5000.0
    assert summary.total_value == 6500.0


def test_cash_counts_toward_portfolio_value_in_history(store):
    """The valuation identity from the brief: holdings + cash."""
    from backend import portfolio_history as ph

    positions = db.list_positions()
    assert ph.cash_today(positions) == 0.0
    db.create_position(PositionIn(symbol="CASH", name="Sweep", broker="schwab",
                                  asset_type="cash", quantity=2500.0, average_cost=1.0,
                                  current_price=1.0))
    assert ph.cash_today(db.list_positions()) == 2500.0


def test_broker_names_are_normalized_consistently(store):
    """The same dropdown feeds web, mobile web and native, so 'Charles Schwab'
    typed anywhere has to land as one broker, not three."""
    for typed in ("Charles Schwab", "charles schwab", "CHARLES SCHWAB"):
        position = PositionIn(symbol="AAPL", name="Apple", broker=typed,
                              asset_type="stock", quantity=1, average_cost=1.0,
                              current_price=1.0)
        assert position.broker == "charles_schwab"

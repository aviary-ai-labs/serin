"""Per-symbol reconstruction — the composition behind the value series.

``daily_values`` answers what the portfolio was worth; ``daily_composition``
answers what it was made of, from the same rewind. The X-ray's drift report
reads the second, so the two must never diverge: a total that disagrees with
the sum of its parts would put two numbers on screen that cannot both be true.
"""

from __future__ import annotations

from backend import portfolio_history
from backend.models import Position, Transaction
from backend.portfolio_history import composition_history, daily_composition, daily_values

DAYS = ["2026-01-01", "2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07"]

HISTORY = {
    "AAPL": {"dates": DAYS, "closes": [100.0, 100.0, 100.0, 200.0, 200.0]},
    "MSFT": {"dates": DAYS, "closes": [50.0] * len(DAYS)},
}


def position(symbol="AAPL", quantity=10.0, asset_type="stock", market_value=0.0):
    return Position(
        id=1, symbol=symbol, name=symbol, quantity=quantity, broker="manual",
        asset_type=asset_type, currency="USD", market_value=market_value,
    )


def txn(id_, action, quantity, occurred_at, symbol="AAPL", amount=0.0):
    return Transaction(
        id=id_, symbol=symbol, broker="manual", asset_type="stock", action=action,
        quantity=quantity, price=0.0, fee=0.0, occurred_at=occurred_at, amount=amount,
    )


def on(series, day):
    return next(point for point in series if point["date"] == day)


def test_composition_totals_match_the_value_series():
    """The refactor's contract: daily_values is this with the parts dropped."""
    positions = [position(), position(symbol="MSFT", quantity=20.0)]
    transactions = [txn(1, "buy", 10.0, "2026-01-05", amount=-1000.0)]

    parts = daily_composition(positions, transactions, HISTORY)
    totals = daily_values(positions, transactions, HISTORY)

    assert [p["date"] for p in parts] == [t["date"] for t in totals]
    for part, total in zip(parts, totals, strict=True):
        assert round(sum(part["values"].values()), 2) == total["securities"]
        assert round(part["securities"] + part["cash"], 2) == total["total"]


def test_composition_names_each_holdings_value_on_each_day():
    positions = [position(quantity=10.0), position(symbol="MSFT", quantity=20.0)]
    parts = daily_composition(positions, [], HISTORY)

    assert on(parts, "2026-01-02")["values"] == {"AAPL": 1000.0, "MSFT": 1000.0}
    # AAPL doubles on the 6th and nothing was traded, so the book's shape moved
    # without its owner: an even split becomes two-thirds AAPL.
    assert on(parts, "2026-01-06")["values"] == {"AAPL": 2000.0, "MSFT": 1000.0}


def test_a_sold_out_holding_reappears_in_its_own_past():
    """Rewinding a sale puts the shares back — the whole reason drift can be
    reported on the first run rather than a year after it."""
    positions = [position(quantity=0.0), position(symbol="MSFT", quantity=20.0)]
    parts = daily_composition(
        positions, [txn(1, "sell", 10.0, "2026-01-06", amount=2000.0)], HISTORY
    )

    assert on(parts, "2026-01-05")["values"]["AAPL"] == 1000.0
    assert "AAPL" not in on(parts, "2026-01-07")["values"]


def test_composition_history_publishes_coverage_and_a_reliability_floor():
    positions = [position(quantity=10.0)]
    # Buying 40 shares into a holding that is only 10 today: undoing it drives
    # the count below zero, so the ledger and the holdings cannot both be
    # right and the days before it describe a different portfolio.
    out = composition_history(
        positions=positions,
        transactions=[txn(1, "buy", 40.0, "2026-01-06", amount=-8000.0)],
        history=HISTORY,
        splits={},
    )

    assert out["available"] is True
    assert out["coverage"]["quality"] != "complete"  # no deposits recorded
    assert out["conflicting_symbols"] == ["AAPL"]
    assert out["reliable_from"] == "2026-01-07"


def test_composition_history_reports_absence_rather_than_an_empty_series():
    out = composition_history(
        positions=[position()], transactions=[], history={}, splits={}
    )
    assert out["available"] is False
    assert "history" in out["reason"].lower()
    assert out["coverage"]["quality"] == "holdings_only"


def test_reconstruction_inputs_are_shared_by_both_entry_points():
    """One defaulting path, so the chart and the drift panel can never be
    reconstructed from different baskets."""
    seen = []
    original = portfolio_history._reconstruction_inputs

    def spy(*args):
        seen.append(args)
        return original(*args)

    portfolio_history._reconstruction_inputs = spy
    try:
        positions = [position(quantity=10.0)]
        composition_history(positions=positions, transactions=[], history=HISTORY, splits={})
        portfolio_history.portfolio_performance(
            positions=positions, transactions=[], history=HISTORY, splits={}
        )
    finally:
        portfolio_history._reconstruction_inputs = original
    assert len(seen) == 2

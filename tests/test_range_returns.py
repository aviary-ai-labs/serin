"""Return per dashboard range, measured the way the industry measures it.

A withdrawal is not a loss. Time-weighted return breaks the series at every
external flow so the owner's own transfers cannot masquerade as performance —
which is the whole reason a portfolio that earned $22,645 while $145,000 was
withdrawn was being shown as down 12.62%.
"""

from __future__ import annotations

import pytest
from backend import portfolio_history


def series(*pairs):
    return [{"date": d, "total": v, "securities": v, "cash": 0.0} for d, v in pairs]


def test_a_withdrawal_is_not_a_loss():
    """The question this answers. The account fell from 1,000 to 900 only
    because 150 was taken out; the investments gained 50."""
    points = series(("2026-01-01", 1000.0), ("2026-06-01", 1050.0),
                    ("2026-08-31", 900.0))
    flows = {"2026-08-31": -150.0}
    result = portfolio_history.returns_by_range(points, flows)["all"]

    assert result["value_change"] == pytest.approx(-100.0)
    assert result["net_external"] == pytest.approx(-150.0)
    assert result["market_change"] == pytest.approx(50.0)
    assert result["twr_pct"] > 0, "a withdrawal was counted as a loss"
    assert result["twr_pct"] == pytest.approx(5.0)


def test_a_deposit_is_not_a_gain():
    """The mirror error, and the more flattering one."""
    points = series(("2026-01-01", 1000.0), ("2026-08-31", 2000.0))
    result = portfolio_history.returns_by_range(points, {"2026-08-31": 1000.0})["all"]
    assert result["value_change"] == pytest.approx(1000.0)
    assert result["twr_pct"] == pytest.approx(0.0), "saving was counted as skill"


def test_a_plain_gain_with_no_flows_is_just_the_gain():
    points = series(("2026-01-01", 1000.0), ("2026-08-31", 1100.0))
    result = portfolio_history.returns_by_range(points, {})["all"]
    assert result["twr_pct"] == pytest.approx(10.0)
    assert result["market_change"] == pytest.approx(100.0)


def test_ytd_anchors_on_the_previous_close_not_the_first_of_the_year():
    """Anchoring on the first close *inside* the year discards the move on
    that day. It is worth 2.5 points of a year's return in one real book, and
    is why the same period read 5.01% on the dashboard and 2.5% in the X-ray."""
    points = series(("2025-12-31", 1000.0), ("2026-01-02", 1100.0),
                    ("2026-08-31", 1210.0))
    result = portfolio_history.returns_by_range(points, {})["ytd"]
    assert result["from"] == "2025-12-31", "measured from inside the window"
    assert result["twr_pct"] == pytest.approx(21.0)


def test_ytd_falls_back_to_the_series_start_when_it_begins_mid_year():
    points = series(("2026-03-02", 1000.0), ("2026-08-31", 1100.0))
    assert portfolio_history.returns_by_range(points, {})["ytd"]["from"] == "2026-03-02"


def test_each_range_reports_its_own_window():
    points = series(*[(f"2026-08-{d:02d}", 1000.0 + d) for d in range(1, 32)])
    out = portfolio_history.returns_by_range(points, {})
    assert out["1w"]["from"] > out["1m"]["from"] >= out["all"]["from"]
    assert out["all"]["to"] == out["1w"]["to"] == "2026-08-31"


def test_flows_before_the_window_do_not_count_against_it():
    """A withdrawal in March is not part of an August week."""
    points = series(*[(f"2026-08-{d:02d}", 1000.0) for d in range(1, 32)])
    out = portfolio_history.returns_by_range(points, {"2026-03-31": -50_000.0})
    assert out["1w"]["net_external"] == pytest.approx(0.0)
    assert out["1w"]["market_change"] == pytest.approx(0.0)


def test_a_series_too_short_to_measure_reports_nothing():
    assert portfolio_history.returns_by_range(series(("2026-08-31", 10.0)), {}) == {}


# --- one account at a time -------------------------------------------------


class Pos:
    def __init__(self, symbol, quantity, broker, market_value=0.0,
                 asset_type="stock"):
        self.symbol, self.quantity, self.broker = symbol, quantity, broker
        self.market_value, self.asset_type = market_value, asset_type


def _txn(action, day, broker, symbol="", qty=0.0, price=0.0, amount=0.0):
    from backend.models import Transaction

    return Transaction(
        id=1, symbol=symbol, broker=broker, asset_type="stock", action=action,
        quantity=qty, price=price, fee=0.0, occurred_at=day, amount=amount,
    )


DAYS = ["2026-01-02", "2026-04-01", "2026-08-31"]
HIST = {"AAPL": {"dates": DAYS, "closes": [100.0, 100.0, 100.0]},
        "MSFT": {"dates": DAYS, "closes": [50.0, 50.0, 50.0]}}


def test_a_broker_is_reconstructed_from_its_own_ledger():
    """Not from today's holdings priced backwards. The fallback showed an
    account that had money withdrawn from it as a clean gain."""
    perf = portfolio_history.portfolio_performance(
        positions=[Pos("AAPL", 100, "robinhood", 10_000.0),
                   Pos("MSFT", 100, "etrade", 5_000.0)],
        transactions=[_txn("withdrawal", "2026-04-01", "robinhood", amount=-4_000.0)],
        history=HIST,
    )
    by_broker = perf["by_broker"]
    assert set(by_broker) == {"robinhood", "etrade"}

    rh = by_broker["robinhood"]["returns"]["all"]
    assert rh["net_external"] == pytest.approx(-4_000.0)
    assert rh["twr_pct"] == pytest.approx(0.0), "a withdrawal read as performance"

    et = by_broker["etrade"]["returns"]["all"]
    assert et["net_external"] == pytest.approx(0.0)


def test_one_brokers_flows_do_not_touch_another():
    perf = portfolio_history.portfolio_performance(
        positions=[Pos("AAPL", 100, "robinhood", 10_000.0),
                   Pos("MSFT", 100, "etrade", 5_000.0)],
        transactions=[_txn("withdrawal", "2026-04-01", "robinhood", amount=-4_000.0)],
        history=HIST,
    )
    assert perf["by_broker"]["etrade"]["net_external"] == 0
    assert perf["by_broker"]["robinhood"]["net_external"] == pytest.approx(-4_000.0)


def test_the_broker_slices_sum_to_the_whole():
    """If they did not, selecting an account would quietly tell a different
    story from the total above it — which is the confusion this replaces."""
    positions = [Pos("AAPL", 100, "robinhood", 10_000.0),
                 Pos("MSFT", 100, "etrade", 5_000.0)]
    perf = portfolio_history.portfolio_performance(
        positions=positions, transactions=[], history=HIST)
    total_end = perf["series"][-1]["total"]
    parts = sum(v["series"][-1]["total"] for v in perf["by_broker"].values())
    assert parts == pytest.approx(total_end)

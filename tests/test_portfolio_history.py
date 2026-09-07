"""The acceptance cases from the performance brief, as tests.

Each one is a claim about what a number must *not* do: a deposit must not
look like skill, a withdrawal must not look like a loss, a sale must not
erase the past. The arithmetic is only interesting because those claims are
easy to get wrong and impossible to notice once shipped.
"""

from __future__ import annotations

import pytest
from backend import portfolio_history as ph
from backend.models import Transaction


def _txn(action, day, *, symbol="", qty=0.0, price=0.0, fee=0.0, amount=None):
    from backend.db import _derive_amount

    return Transaction(
        id=0, symbol=symbol, broker="fidelity", asset_type="stock", action=action,
        quantity=qty, price=price, fee=fee, occurred_at=day,
        amount=amount if amount is not None else _derive_amount(action, qty, price, fee),
    )


class FakePos:
    """Minimal stand-in so these tests exercise arithmetic, not the ORM."""

    def __init__(self, symbol, quantity, market_value=0.0, asset_type="stock",
                 broker="robinhood"):
        self.symbol = symbol
        self.quantity = quantity
        self.market_value = market_value
        self.asset_type = asset_type
        self.broker = broker


def _flat_history(symbol="AAPL", days=None, price=100.0):
    days = days or ["2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05"]
    return {symbol: {"dates": days, "closes": [price] * len(days)}}


DAYS = ["2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05"]


# --- external flows must not move TWR -----------------------------------


def test_a_deposit_raises_value_but_not_twr():
    """The headline claim. Adding money makes the portfolio bigger; it does
    not make you a better investor."""
    positions = [FakePos("AAPL", 10, 1000.0), FakePos("CASH", 0, 500.0, "cash")]
    txns = [_txn("deposit", "2026-01-04", price=500.0)]
    result = ph.portfolio_performance(positions, txns, _flat_history())
    assert result["available"]
    assert result["series"][0]["total"] == 1000.0     # before the deposit
    assert result["series"][-1]["total"] == 1500.0    # after it
    assert result["twr_pct"] == pytest.approx(0.0, abs=1e-6)


def test_a_withdrawal_lowers_value_but_not_twr():
    positions = [FakePos("AAPL", 10, 1000.0), FakePos("CASH", 0, 0.0, "cash")]
    txns = [_txn("withdrawal", "2026-01-04", price=300.0)]
    result = ph.portfolio_performance(positions, txns, _flat_history())
    assert result["series"][0]["total"] == 1300.0
    assert result["series"][-1]["total"] == 1000.0
    assert result["twr_pct"] == pytest.approx(0.0, abs=1e-6)


def test_the_legacy_spelling_of_deposit_still_counts():
    """cash_in predates deposit and is already in customers' databases. If it
    stopped being treated as external, their old deposits would silently start
    counting as investment gains."""
    positions = [FakePos("AAPL", 10, 1000.0), FakePos("CASH", 0, 500.0, "cash")]
    txns = [_txn("cash_in", "2026-01-04", price=500.0)]
    result = ph.portfolio_performance(positions, txns, _flat_history())
    assert result["twr_pct"] == pytest.approx(0.0, abs=1e-6)


# --- internal activity must not move TWR --------------------------------


def test_a_transfer_between_tracked_accounts_changes_nothing():
    positions = [FakePos("AAPL", 10, 1000.0), FakePos("CASH", 0, 0.0, "cash")]
    txns = [_txn("transfer", "2026-01-04", price=500.0)]
    result = ph.portfolio_performance(positions, txns, _flat_history())
    assert result["twr_pct"] == pytest.approx(0.0, abs=1e-6)
    assert result["series"][0]["total"] == result["series"][-1]["total"]


def test_buying_is_not_a_contribution():
    """Cash becomes shares. The portfolio is worth the same either side, so a
    buy must not register as money arriving."""
    positions = [FakePos("AAPL", 10, 1000.0), FakePos("CASH", 0, 0.0, "cash")]
    txns = [_txn("buy", "2026-01-04", symbol="AAPL", qty=10, price=100.0)]
    result = ph.portfolio_performance(positions, txns, _flat_history())
    for point in result["series"]:
        assert point["total"] == pytest.approx(1000.0)
    assert result["twr_pct"] == pytest.approx(0.0, abs=1e-6)
    assert result["net_external"] == 0.0


# --- income and cost do move return --------------------------------------


def test_a_dividend_increases_return():
    positions = [FakePos("AAPL", 10, 1000.0), FakePos("CASH", 0, 50.0, "cash")]
    txns = [_txn("dividend", "2026-01-04", symbol="AAPL", price=50.0)]
    result = ph.portfolio_performance(positions, txns, _flat_history())
    assert result["twr_pct"] > 0


def test_a_fee_decreases_return():
    positions = [FakePos("AAPL", 10, 1000.0), FakePos("CASH", 0, 0.0, "cash")]
    txns = [_txn("fee", "2026-01-04", price=25.0)]
    result = ph.portfolio_performance(positions, txns, _flat_history())
    assert result["twr_pct"] < 0


def test_tax_decreases_return_like_a_fee():
    positions = [FakePos("AAPL", 10, 1000.0), FakePos("CASH", 0, 0.0, "cash")]
    txns = [_txn("tax", "2026-01-04", price=25.0)]
    result = ph.portfolio_performance(positions, txns, _flat_history())
    assert result["twr_pct"] < 0


# --- the survivor-bias case ----------------------------------------------


def test_a_fully_sold_position_keeps_its_history():
    """The brief's central complaint. NFLX was held and sold; it is worth
    nothing today, but the days it was held must still be valued — otherwise
    the chart quietly redraws as though it was never owned."""
    positions = [FakePos("NFLX", 0, 0.0), FakePos("CASH", 0, 1200.0, "cash")]
    txns = [_txn("sell", "2026-01-04", symbol="NFLX", qty=10, price=120.0)]
    history = {"NFLX": {"dates": DAYS, "closes": [100.0, 100.0, 120.0, 120.0]}}
    result = ph.portfolio_performance(positions, txns, history)
    early = result["series"][0]
    assert early["securities"] == pytest.approx(1000.0), "sold holding vanished from history"
    assert result["series"][-1]["securities"] == 0.0
    assert result["series"][-1]["cash"] == 1200.0


def test_selling_at_a_gain_shows_as_a_gain_not_a_withdrawal():
    positions = [FakePos("NFLX", 0, 0.0), FakePos("CASH", 0, 1200.0, "cash")]
    txns = [_txn("sell", "2026-01-04", symbol="NFLX", qty=10, price=120.0)]
    history = {"NFLX": {"dates": DAYS, "closes": [100.0, 100.0, 120.0, 120.0]}}
    result = ph.portfolio_performance(positions, txns, history)
    assert result["net_external"] == 0.0
    assert result["twr_pct"] > 0


# --- coverage -------------------------------------------------------------


def test_holdings_with_no_transactions_are_labelled_estimated():
    positions = [FakePos("AAPL", 10, 1000.0)]
    result = ph.portfolio_performance(positions, [], _flat_history())
    assert result["coverage"]["quality"] == "holdings_only"
    assert result["coverage"]["estimated"] is True
    assert "estimated" in result["coverage"]["message"].lower()


def test_trades_without_cash_activity_say_so():
    positions = [FakePos("AAPL", 10, 1000.0), FakePos("CASH", 0, 100.0, "cash")]
    txns = [_txn("buy", "2026-01-03", symbol="AAPL", qty=10, price=100.0)]
    result = ph.portfolio_performance(positions, txns, _flat_history())
    assert result["coverage"]["quality"] == "missing_cash_activity"
    assert result["coverage"]["estimated"] is True


def test_a_complete_history_is_not_labelled_estimated():
    positions = [FakePos("AAPL", 10, 1000.0), FakePos("CASH", 0, 100.0, "cash")]
    txns = [
        _txn("deposit", "2026-01-02", price=1100.0),
        _txn("buy", "2026-01-03", symbol="AAPL", qty=10, price=100.0),
    ]
    result = ph.portfolio_performance(positions, txns, _flat_history())
    assert result["coverage"]["quality"] == "complete"
    assert result["coverage"]["estimated"] is False


def test_coverage_reports_the_date_history_begins():
    positions = [FakePos("AAPL", 10, 1000.0)]
    txns = [_txn("buy", "2026-01-03", symbol="AAPL", qty=10, price=100.0)]
    result = ph.portfolio_performance(positions, txns, _flat_history())
    assert result["coverage"]["since"] == "2026-01-03"


def test_untraded_holdings_are_named_in_the_coverage_message():
    positions = [FakePos("AAPL", 10, 1000.0), FakePos("MSFT", 5, 500.0)]
    txns = [_txn("buy", "2026-01-03", symbol="AAPL", qty=10, price=100.0)]
    history = {
        "AAPL": {"dates": DAYS, "closes": [100.0] * 4},
        "MSFT": {"dates": DAYS, "closes": [100.0] * 4},
    }
    result = ph.portfolio_performance(positions, txns, history)
    assert result["coverage"]["symbols_without_trades"] == ["MSFT"]
    assert "MSFT" in result["coverage"]["message"]


# --- MWR ------------------------------------------------------------------


def test_mwr_reflects_the_investor_experience_where_twr_does_not():
    """A deposit just before a rise flatters MWR and leaves TWR alone. That
    difference is the whole reason both are reported."""
    positions = [FakePos("AAPL", 10, 1200.0), FakePos("CASH", 0, 0.0, "cash")]
    txns = [_txn("deposit", "2026-01-03", price=500.0)]
    history = {"AAPL": {"dates": DAYS, "closes": [100.0, 100.0, 120.0, 120.0]}}
    result = ph.portfolio_performance(positions, txns, history)
    assert result["mwr_period_pct"] is not None
    assert result["twr_pct"] is not None


def test_no_history_is_reported_rather_than_guessed():
    result = ph.portfolio_performance([FakePos("AAPL", 1, 100.0)], [], {})
    assert result["available"] is False
    assert "coverage" in result


def test_a_zero_value_day_does_not_divide_by_zero():
    """An account that starts empty has no capital to earn a return on."""
    positions = [FakePos("AAPL", 10, 1000.0), FakePos("CASH", 0, 0.0, "cash")]
    txns = [_txn("deposit", "2026-01-03", price=1000.0),
            _txn("buy", "2026-01-03", symbol="AAPL", qty=10, price=100.0)]
    result = ph.portfolio_performance(positions, txns, _flat_history())
    assert result["available"]
    assert result["twr_pct"] is not None


# --- the database path ----------------------------------------------------
# Everything above passes positions in directly, which bypasses the one line
# that decides whether closed holdings are even fetched. That line is the
# realistic regression — list_positions() defaults to open holdings only — so
# it needs a test that goes through the database.


@pytest.fixture
def store(tmp_path):
    from backend import db

    db.set_db_path(tmp_path / "history.db")
    db.init_db()
    return db


def test_history_is_reconstructed_from_the_ledger_not_the_position_rows(store):
    """A stronger property than "closed positions are kept": the ledger alone
    is sufficient. Undoing a sale puts the shares back whether or not a row
    for that holding still exists, so history survives even a position that
    was deleted outright by some older version of Serin.

    (This is why the earlier mutation test could not break it — the position
    row is not load-bearing for valuation. It still matters for coverage,
    which reports what is *held* rather than what was traded.)
    """
    assert store.list_positions(include_closed=True) == [], "no position rows at all"

    history = {"NFLX": {"dates": DAYS, "closes": [100.0, 100.0, 120.0, 120.0]}}
    txns = [_txn("sell", "2026-01-04", symbol="NFLX", qty=10, price=120.0)]
    result = ph.portfolio_performance(None, txns, history)
    assert result["available"]
    assert result["series"][0]["securities"] == pytest.approx(1000.0), (
        "the ledger did not reconstruct a holding that no longer has a row"
    )
    assert result["series"][-1]["securities"] == 0.0


def test_coverage_counts_a_closed_holding_that_was_never_traded(store):
    """Where the position row *does* matter. A holding with no trades behind
    it can only be back-priced, and coverage has to say so — which means it
    has to see closed rows, not just open ones."""
    from backend.models import PositionIn

    sold = store.create_position(
        PositionIn(symbol="NFLX", name="Netflix", broker="fidelity", asset_type="stock",
                   quantity=10, average_cost=100.0, current_price=120.0)
    )
    store.update_position(
        sold.id,
        PositionIn(symbol="NFLX", name="Netflix", broker="fidelity", asset_type="stock",
                   quantity=0, average_cost=100.0, current_price=120.0),
    )
    everything = store.list_positions(include_closed=True)
    assert [p.symbol for p in everything] == ["NFLX"]
    cover = ph.coverage(everything, [])
    assert cover["quality"] == "holdings_only"
    assert cover["estimated"] is True


def test_trades_with_no_deposits_at_all_are_not_called_complete():
    """Buying requires money to have arrived. If no deposit is on record the
    cash side is unexplained, even for someone who tracks no cash balance —
    and calling that "complete" is exactly the false precision the brief
    warns against."""
    positions = [FakePos("AAPL", 10, 1000.0)]
    txns = [_txn("buy", "2026-01-03", symbol="AAPL", qty=10, price=100.0)]
    result = ph.portfolio_performance(positions, txns, _flat_history())
    assert result["coverage"]["quality"] == "missing_cash_activity"
    assert result["coverage"]["estimated"] is True

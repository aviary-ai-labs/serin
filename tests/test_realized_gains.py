"""Realized gains, and the number that must never be reported as gain.

A naive FIFO pass over a partial statement claimed $259,205 of realized gains
on a real portfolio. $210,172 of that was proceeds from sales whose purchases
predate the imported ledger — shares with no cost basis on record, counted as
if they had cost nothing. That figure is worse than showing nothing, because it
looks like an answer.

Everything here exists to keep those two quantities apart.
"""

from __future__ import annotations

import pytest
from backend import db, realized
from backend.models import TransactionIn


@pytest.fixture
def ledger(tmp_path):
    db.set_db_path(tmp_path / "realized.db")
    db.init_db()
    return db


def add(action, symbol="TQQQ", quantity=0.0, price=0.0, when="2026-03-01", fee=0.0):
    return db.create_transaction(TransactionIn(
        symbol=symbol, broker="robinhood", action=action, quantity=quantity,
        price=price, fee=fee, occurred_at=when,
    ))


def row_for(result, symbol):
    return next((r for r in result["by_symbol"] if r["symbol"] == symbol), None)


# --- the matched case ------------------------------------------------------


def test_a_complete_round_trip_reports_its_gain(ledger):
    add("buy", quantity=100, price=50.0, when="2026-01-05")
    add("sell", quantity=100, price=72.0, when="2026-06-05")
    result = realized.realized_gains()
    assert row_for(result, "TQQQ")["realized"] == pytest.approx(2200.0)
    assert result["totals"]["unmatched_proceeds"] == 0
    assert result["coverage"]["complete"] is True


def test_lots_are_consumed_first_in_first_out(ledger):
    add("buy", quantity=10, price=10.0, when="2026-01-05")
    add("buy", quantity=10, price=20.0, when="2026-02-05")
    add("sell", quantity=10, price=30.0, when="2026-03-05")
    # FIFO takes the $10 lot: 10 x (30 - 10) = 200. LIFO would say 100.
    assert row_for(realized.realized_gains(), "TQQQ")["realized"] == pytest.approx(200.0)


def test_a_sale_spanning_two_lots_uses_both_costs(ledger):
    add("buy", quantity=10, price=10.0, when="2026-01-05")
    add("buy", quantity=10, price=20.0, when="2026-02-05")
    add("sell", quantity=15, price=30.0, when="2026-03-05")
    # 10 x (30-10) + 5 x (30-20) = 200 + 50
    assert row_for(realized.realized_gains(), "TQQQ")["realized"] == pytest.approx(250.0)


def test_a_partly_sold_lot_keeps_its_remainder_for_the_next_sale(ledger):
    add("buy", quantity=100, price=10.0, when="2026-01-05")
    add("sell", quantity=40, price=20.0, when="2026-02-05")
    add("sell", quantity=60, price=30.0, when="2026-03-05")
    # 40 x 10 + 60 x 20
    assert row_for(realized.realized_gains(), "TQQQ")["realized"] == pytest.approx(1600.0)


def test_a_loss_is_reported_as_a_loss(ledger):
    add("buy", quantity=100, price=30.0, when="2026-01-05")
    add("sell", quantity=100, price=20.0, when="2026-06-05")
    assert row_for(realized.realized_gains(), "TQQQ")["realized"] == pytest.approx(-1000.0)


def test_the_sale_fee_reduces_the_gain(ledger):
    add("buy", quantity=100, price=50.0, when="2026-01-05")
    add("sell", quantity=100, price=60.0, when="2026-06-05", fee=12.5)
    assert row_for(realized.realized_gains(), "TQQQ")["realized"] == pytest.approx(987.5)


# --- the case that must never become "gain" --------------------------------


def test_a_sale_with_no_recorded_purchase_is_proceeds_not_profit(ledger):
    """The AFRM shape: 519 shares sold, bought before the ledger begins."""
    add("sell", symbol="AFRM", quantity=519, price=84.0, when="2026-07-09")
    result = realized.realized_gains()
    row = row_for(result, "AFRM")
    assert row["realized"] == 0, "a sale with no cost basis was counted as gain"
    assert row["unmatched_shares"] == pytest.approx(519)
    assert row["unmatched_proceeds"] == pytest.approx(43596.0)
    assert result["coverage"]["complete"] is False
    assert "AFRM" in result["coverage"]["symbols_missing_basis"]


def test_unmatched_proceeds_stay_out_of_the_net(ledger):
    """The single most important assertion here. Folding them in is what
    produced the $259,205 figure."""
    add("buy", quantity=10, price=10.0, when="2026-01-05")
    add("sell", quantity=10, price=20.0, when="2026-02-05")          # +100 real
    add("sell", symbol="AFRM", quantity=100, price=84.0, when="2026-07-09")
    totals = realized.realized_gains()["totals"]
    assert totals["realized"] == pytest.approx(100.0)
    assert totals["unmatched_proceeds"] == pytest.approx(8400.0)
    assert totals["net"] == pytest.approx(100.0), "unmatched proceeds leaked into net"


def test_a_sale_larger_than_the_lots_splits_across_both_buckets(ledger):
    """Partial coverage is the common case, not an edge one: the ledger holds
    some of the history and not the rest."""
    add("buy", quantity=40, price=10.0, when="2026-01-05")
    add("sell", quantity=100, price=20.0, when="2026-06-05")
    row = row_for(realized.realized_gains(), "TQQQ")
    assert row["realized"] == pytest.approx(400.0)        # the 40 that matched
    assert row["unmatched_shares"] == pytest.approx(60)
    assert row["unmatched_proceeds"] == pytest.approx(1200.0)


def test_a_fee_is_not_charged_against_shares_with_no_basis(ledger):
    """Attaching it there would imply a gain figure for shares that have none."""
    add("sell", symbol="AFRM", quantity=100, price=84.0, when="2026-07-09", fee=5.0)
    row = row_for(realized.realized_gains(), "AFRM")
    assert row["realized"] == 0


# --- income, which needs no cost basis to be true --------------------------


def test_dividends_and_interest_are_counted_separately(ledger):
    add("dividend", symbol="BABA", price=538.98, when="2026-07-13")
    add("interest", symbol="", price=935.71, when="2026-07-31")
    totals = realized.realized_gains()["totals"]
    assert totals["income"] == pytest.approx(1474.69)
    assert totals["realized"] == 0, "income was mixed into matched gains"


def test_fees_and_tax_reduce_the_net(ledger):
    add("buy", quantity=10, price=10.0, when="2026-01-05")
    add("sell", quantity=10, price=20.0, when="2026-02-05")
    add("fee", symbol="", price=50.0, when="2026-03-05")
    totals = realized.realized_gains()["totals"]
    assert totals["costs"] == pytest.approx(50.0)
    assert totals["net"] == pytest.approx(50.0)


# --- the tax year ----------------------------------------------------------


def test_the_year_filters_disposals_not_purchases(ledger):
    """A lot bought in December and sold in January belongs to January's year,
    and must still find its cost. Filtering buys too would orphan it."""
    add("buy", quantity=100, price=50.0, when="2025-12-20")
    add("sell", quantity=100, price=60.0, when="2026-01-15")
    assert realized.realized_gains("2026")["totals"]["realized"] == pytest.approx(1000.0)
    assert realized.realized_gains("2025")["totals"]["realized"] == 0


def test_a_prior_years_sale_is_excluded_from_this_year(ledger):
    add("buy", quantity=100, price=10.0, when="2025-01-05")
    add("sell", quantity=100, price=20.0, when="2025-06-05")
    add("buy", quantity=100, price=10.0, when="2026-01-05")
    add("sell", quantity=100, price=30.0, when="2026-06-05")
    assert realized.realized_gains("2026")["totals"]["realized"] == pytest.approx(2000.0)
    assert realized.realized_gains("2025")["totals"]["realized"] == pytest.approx(1000.0)
    assert realized.realized_gains()["totals"]["realized"] == pytest.approx(3000.0)


def test_available_years_lists_only_years_with_something_realized(ledger):
    add("buy", quantity=10, price=10.0, when="2024-01-05")   # a buy alone is not a year
    add("sell", quantity=10, price=20.0, when="2026-06-05")
    add("dividend", symbol="BABA", price=5.0, when="2025-03-01")
    assert realized.available_years() == ["2026", "2025"]


# --- shape -----------------------------------------------------------------


def test_symbols_with_nothing_realized_are_not_listed(ledger):
    """An open position that was never sold has no realized result, and a row
    of zeros for every holding buries the ones that matter."""
    add("buy", quantity=100, price=50.0, when="2026-01-05")
    assert realized.realized_gains()["by_symbol"] == []


def test_an_empty_ledger_answers_zeroes_rather_than_failing(ledger):
    result = realized.realized_gains()
    assert result["by_symbol"] == []
    assert result["totals"]["net"] == 0
    assert result["coverage"]["complete"] is True


def test_the_basis_is_declared(ledger):
    """FIFO is an assumption, not a fact about the account. Saying so is the
    difference between a figure someone can check and one they must trust."""
    assert realized.realized_gains()["basis"] == "fifo"


# --- drilling into a headline figure ---------------------------------------
#
# A summary card invites the question "which trades?" and leaves nowhere to
# ask it. The property that matters is that the answer adds up: detail and
# headline walk the same lots, so a total that disagreed with the rows under
# it would read as broken numbers rather than broken code.


def test_the_gains_detail_sums_to_the_headline(ledger):
    add("buy", "TQQQ", 100, 50.0, when="2026-01-05")
    add("sell", "TQQQ", 60, 80.0, when="2026-03-05")
    add("buy", "HOOD", 10, 20.0, when="2026-02-01")
    add("sell", "HOOD", 10, 15.0, when="2026-04-01")

    summary = realized.realized_gains()
    detail = realized.realized_detail("gains")
    assert detail["total"] == pytest.approx(summary["totals"]["realized"])
    assert detail["count"] == 2


def test_the_income_detail_sums_to_the_headline(ledger):
    add("dividend", "TQQQ", price=120.0, when="2026-02-01")
    add("interest", "", price=45.5, when="2026-03-01")
    summary = realized.realized_gains()
    detail = realized.realized_detail("income")
    assert detail["total"] == pytest.approx(summary["totals"]["income"])
    assert detail["total"] == pytest.approx(165.5)


def test_costs_drill_in_as_negative_contributions(ledger):
    """They reduce the net, so they have to carry their sign into a list that
    is summed — otherwise the rows contradict the total above them."""
    add("fee", "", price=10.0, when="2026-02-01")
    add("tax", "", price=5.0, when="2026-03-01")
    detail = realized.realized_detail("costs")
    assert detail["total"] == pytest.approx(-15.0)
    assert all(line["contribution"] < 0 for line in detail["lines"])


def test_the_net_detail_sums_to_the_net(ledger):
    """The card people are most likely to click, and the one whose arithmetic
    is least obvious: gains plus income minus costs."""
    add("buy", "TQQQ", 100, 50.0, when="2026-01-05")
    add("sell", "TQQQ", 100, 80.0, when="2026-03-05")
    add("dividend", "TQQQ", price=120.0, when="2026-02-01")
    add("fee", "", price=10.0, when="2026-02-02")

    summary = realized.realized_gains()
    detail = realized.realized_detail("net")
    assert detail["total"] == pytest.approx(summary["totals"]["net"])
    assert detail["total"] == pytest.approx(3000.0 + 120.0 - 10.0)


def test_a_sale_with_no_cost_basis_is_absent_from_the_gains_drill_in(ledger):
    """It contributed nothing to that figure. Listing it would imply it did,
    which is the same error as counting its proceeds as profit."""
    add("sell", "AFRM", 519, 84.0, when="2026-07-09")
    detail = realized.realized_detail("gains")
    assert detail["lines"] == []
    assert detail["total"] == pytest.approx(0.0)


def test_a_partly_matched_sale_shows_only_the_matched_part(ledger):
    add("buy", "TQQQ", 40, 10.0, when="2026-01-05")
    add("sell", "TQQQ", 100, 20.0, when="2026-06-05")
    line = realized.realized_detail("gains")["lines"][0]
    assert line["matched_shares"] == pytest.approx(40)
    assert line["unmatched_shares"] == pytest.approx(60)
    assert line["cost_basis"] == pytest.approx(400.0)
    assert line["contribution"] == pytest.approx(400.0)   # 40 * (20 - 10)


def test_the_year_filter_reaches_the_drill_in(ledger):
    add("buy", "TQQQ", 100, 50.0, when="2025-01-05")
    add("sell", "TQQQ", 50, 80.0, when="2025-06-05")
    add("sell", "TQQQ", 50, 90.0, when="2026-06-05")
    assert realized.realized_detail("gains", "2026")["count"] == 1
    assert realized.realized_detail("gains", "2026")["total"] == pytest.approx(2000.0)


def test_lines_lead_with_what_moved_the_number_most(ledger):
    add("buy", "A", 10, 1.0, when="2026-01-01")
    add("sell", "A", 10, 2.0, when="2026-02-01")      # +10
    add("buy", "B", 10, 1.0, when="2026-01-01")
    add("sell", "B", 10, 100.0, when="2026-02-01")    # +990
    symbols = [line["symbol"] for line in realized.realized_detail("gains")["lines"]]
    assert symbols == ["B", "A"]


def test_an_unknown_figure_returns_nothing_rather_than_everything(ledger):
    add("buy", "TQQQ", 10, 1.0, when="2026-01-01")
    add("sell", "TQQQ", 10, 2.0, when="2026-02-01")
    assert realized.realized_detail("bogus")["lines"] == []


# --- lots live in the account that bought them -----------------------------
#
# FIFO pooled every lot by symbol, so a sale at one broker could consume a
# cheaper lot bought years earlier at another and book a gain that happened in
# neither account. Eight symbols were traded at two brokers on the book that
# exposed this, so it was live, not theoretical.


def broker_add(action, symbol, quantity, price, when, broker, fee=0.0):
    return db.create_transaction(TransactionIn(
        symbol=symbol, broker=broker, action=action, quantity=quantity,
        price=price, fee=fee, occurred_at=when,
    ))


def test_a_sale_cannot_consume_another_accounts_lot(ledger):
    """The bug. GOOG bought cheap at Robinhood in 2016 and sold at E*Trade in
    2026 is not a $200/share gain — the E*Trade account never held the cheap
    shares, and nothing says what its own cost."""
    broker_add("buy", "GOOG", 100, 50.0, "2016-06-09", "robinhood")
    broker_add("sell", "GOOG", 100, 250.0, "2026-01-02", "etrade")

    result = realized.realized_gains()
    assert result["totals"]["realized"] == pytest.approx(0.0), \
        "an E*Trade sale claimed a Robinhood cost basis"
    assert result["totals"]["unmatched_proceeds"] == pytest.approx(25_000.0)
    assert result["totals"]["unmatched_shares"] == pytest.approx(100)


def test_each_account_matches_against_its_own_purchases(ledger):
    """Both accounts hold the symbol and both trade it. Each gain has to come
    from its own lots, not from whichever happens to be cheapest."""
    broker_add("buy", "AAPL", 10, 100.0, "2026-01-05", "robinhood")
    broker_add("buy", "AAPL", 10, 200.0, "2026-01-06", "etrade")
    broker_add("sell", "AAPL", 10, 300.0, "2026-06-01", "etrade")

    result = realized.realized_gains()
    # E*Trade's own lot cost 200, so the gain is 1,000 — not the 2,000 that
    # pooling would produce by reaching for Robinhood's cheaper shares.
    assert result["totals"]["realized"] == pytest.approx(1_000.0)
    assert result["totals"]["unmatched_proceeds"] == 0


def test_the_drill_in_shows_the_accounts_own_cost_basis(ledger):
    broker_add("buy", "AAPL", 10, 100.0, "2026-01-05", "robinhood")
    broker_add("buy", "AAPL", 10, 200.0, "2026-01-06", "etrade")
    broker_add("sell", "AAPL", 10, 300.0, "2026-06-01", "etrade")

    line = realized.realized_detail("gains")["lines"][0]
    assert line["broker"] == "etrade"
    assert line["cost_basis"] == pytest.approx(2_000.0)
    assert line["contribution"] == pytest.approx(1_000.0)


def test_fifo_still_orders_within_one_account(ledger):
    """Scoping by account must not quietly become average cost."""
    broker_add("buy", "TQQQ", 10, 40.0, "2026-01-05", "robinhood")
    broker_add("buy", "TQQQ", 10, 70.0, "2026-02-05", "robinhood")
    broker_add("sell", "TQQQ", 10, 100.0, "2026-06-05", "robinhood")
    # Oldest lot first: 10 * (100 - 40), not the 300 average cost would give.
    assert realized.realized_gains()["totals"]["realized"] == pytest.approx(600.0)


def test_shares_moved_between_accounts_read_as_proceeds_not_profit(ledger):
    """The deliberate cost of the fix. The receiving account has no record of
    what the shares cost, and borrowing the sending account's number would be
    a guess wearing the clothes of a fact — so it reports the hole instead."""
    broker_add("buy", "META", 50, 300.0, "2025-01-05", "robinhood")
    broker_add("sell", "META", 50, 600.0, "2026-06-05", "etrade")
    result = realized.realized_gains()
    assert result["totals"]["realized"] == 0
    assert result["coverage"]["symbols_missing_basis"] == ["META"]


def test_uncovered_sales_are_reported_per_account_with_its_own_start(ledger):
    """A single global "history begins on 2016-06-09" is right for the earliest
    broker and wrong for every other. It named E*Trade holdings against
    Robinhood's date, which sends someone hunting for the wrong paperwork."""
    broker_add("buy", "TQQQ", 1, 10.0, "2016-06-09", "robinhood")
    broker_add("sell", "TQQQ", 51, 20.0, "2026-05-01", "robinhood")
    broker_add("buy", "AVGO", 1, 100.0, "2024-09-30", "etrade")
    broker_add("sell", "AVGO", 101, 300.0, "2025-11-06", "etrade")

    by_broker = {e["broker"]: e
                 for e in realized.realized_gains()["coverage"]["by_broker"]}
    assert set(by_broker) == {"robinhood", "etrade"}
    assert by_broker["etrade"]["since"] == "2024-09-30"
    assert by_broker["robinhood"]["since"] == "2016-06-09"
    assert by_broker["etrade"]["symbols"] == ["AVGO"]
    assert by_broker["etrade"]["proceeds"] == pytest.approx(30_000.0)   # 100 * 300
    # Largest hole first: it is the one worth chasing statements for.
    assert realized.realized_gains()["coverage"]["by_broker"][0]["broker"] == "etrade"


def test_a_fully_covered_book_reports_no_per_account_holes(ledger):
    broker_add("buy", "TQQQ", 100, 10.0, "2026-01-05", "robinhood")
    broker_add("sell", "TQQQ", 100, 20.0, "2026-06-05", "robinhood")
    assert realized.realized_gains()["coverage"]["by_broker"] == []


# --- an option contract is a hundred shares --------------------------------


def option_txn(action, symbol, contracts, price, when, broker="robinhood"):
    return db.create_transaction(TransactionIn(
        symbol=symbol, broker=broker, asset_type="option", action=action,
        quantity=contracts, price=price, occurred_at=when,
    ))


def test_an_option_result_is_priced_per_contract_not_per_share(ledger):
    """Calibrated against the broker's own figure. Ten MSFT calls bought at
    $21.70 and sold at $25.93 earned $4,230.00 in Robinhood's realized P&L;
    Serin reported $42.30 — exactly a hundredth, once per option, every time."""
    option_txn("buy", "MSFT", 10, 21.70, "2026-04-16")
    option_txn("sell", "MSFT", 10, 25.93, "2026-04-17")
    result = realized.realized_gains()
    assert result["totals"]["realized"] == pytest.approx(4230.00, abs=0.01)


def test_the_three_msft_calls_reconcile_to_the_brokers_total(ledger):
    """The whole of the broker's MSFT year: $11,928.00 across three closes."""
    option_txn("buy", "MSFT", 10, 21.70, "2026-04-16")
    option_txn("sell", "MSFT", 10, 25.93, "2026-04-17")     # +4,230.00
    option_txn("buy", "MSFT", 2, 21.80, "2026-04-23")
    option_txn("buy", "MSFT", 8, 21.75, "2026-04-23")
    option_txn("sell", "MSFT", 10, 25.00, "2026-05-29")     # +3,240.00
    option_txn("buy", "MSFT", 2, 43.30, "2026-04-23")
    option_txn("buy", "MSFT", 8, 43.29, "2026-04-23")
    option_txn("sell", "MSFT", 10, 47.75, "2026-07-30")     # +4,458.00
    assert realized.realized_gains()["totals"]["realized"] == pytest.approx(11928.00, abs=0.01)


def test_shares_are_untouched_by_the_multiplier(ledger):
    broker_add("buy", "TQQQ", 100, 40.0, "2026-01-05", "robinhood")
    broker_add("sell", "TQQQ", 100, 70.0, "2026-06-05", "robinhood")
    assert realized.realized_gains()["totals"]["realized"] == pytest.approx(3000.0)


def test_a_call_does_not_match_against_a_share_lot_of_the_same_ticker(ledger):
    """MSFT shares and MSFT calls are the same ticker and entirely different
    instruments. Matching one against the other prices it in the wrong units."""
    broker_add("buy", "MSFT", 10, 400.0, "2026-01-05", "robinhood")   # shares
    option_txn("sell", "MSFT", 10, 25.93, "2026-04-17")               # calls
    result = realized.realized_gains()
    assert result["totals"]["realized"] == 0, "a call was covered by a share lot"
    assert result["totals"]["unmatched_proceeds"] == pytest.approx(25_930.0)


def test_option_proceeds_are_reported_in_real_dollars(ledger):
    option_txn("sell", "MSFT", 10, 25.93, "2026-04-17")
    totals = realized.realized_gains()["totals"]
    assert totals["unmatched_proceeds"] == pytest.approx(25_930.0)


def test_option_rows_written_before_the_multiplier_are_restated(ledger):
    """amount is what a row did to the cash balance, and the reconstructed
    balance reads it. A $21,700 option purchase recorded as $217 leaves the
    cash side of every chart wrong until the stored rows are corrected."""
    txn = option_txn("buy", "MSFT", 10, 21.70, "2026-04-16")
    with db.connect() as conn:                      # simulate the old write
        conn.execute("UPDATE transactions SET amount = ? WHERE id = ?", (-217.0, txn.id))

    preview = db.repair_option_amounts(dry_run=True)
    assert preview["restated"] == 1
    assert preview["cash_delta"] == pytest.approx(-21_483.0)   # -21,700 from -217

    db.repair_option_amounts(dry_run=False)
    restated = next(t for t in db.list_transactions(limit=10) if t.id == txn.id)
    assert float(restated.amount) == pytest.approx(-21_700.0)
    assert db.repair_option_amounts(dry_run=True)["restated"] == 0, "not idempotent"


def test_the_repair_leaves_share_rows_alone(ledger):
    broker_add("buy", "TQQQ", 100, 40.0, "2026-01-05", "robinhood")
    assert db.repair_option_amounts(dry_run=True)["restated"] == 0


def option_with_contract(action, symbol, contracts, price, when, contract,
                         broker="robinhood"):
    return db.create_transaction(TransactionIn(
        symbol=symbol, broker=broker, asset_type="option", action=action,
        quantity=contracts, price=price, occurred_at=when, notes=contract,
    ))


def test_each_option_contract_keeps_its_own_lots(ledger):
    """Calibrated against the broker line by line. Three MSFT calls bought two
    at a time on one day: pooling them by ticker let the $450 call's sale
    consume the $430 call's basis, and the two sales came out at -$18,292 and
    +$25,990 against the broker's +$3,240 and +$4,458. The year's total was
    right either way, which is what made it invisible."""
    option_with_contract("buy", "MSFT", 8, 43.29, "2026-04-23", "MSFT 12/18/2026 Call $430.00")
    option_with_contract("buy", "MSFT", 2, 43.30, "2026-04-23", "MSFT 12/18/2026 Call $430.00")
    option_with_contract("buy", "MSFT", 8, 21.75, "2026-04-23", "MSFT 8/21/2026 Call $450.00")
    option_with_contract("buy", "MSFT", 2, 21.80, "2026-04-23", "MSFT 8/21/2026 Call $450.00")
    option_with_contract("sell", "MSFT", 10, 25.00, "2026-05-29", "MSFT 8/21/2026 Call $450.00")
    option_with_contract("sell", "MSFT", 10, 47.75, "2026-07-30", "MSFT 12/18/2026 Call $430.00")

    by_date = {l["date"]: l["contribution"]
               for l in realized.realized_detail("gains")["lines"]}
    assert by_date["2026-05-29"] == pytest.approx(3240.00, abs=0.01)
    assert by_date["2026-07-30"] == pytest.approx(4458.00, abs=0.01)


def test_an_option_with_no_contract_recorded_still_matches_by_ticker(ledger):
    """The fallback. Statements that name no contract behave as before rather
    than every lot becoming its own island."""
    option_txn("buy", "MSFT", 10, 21.70, "2026-04-16")
    option_txn("sell", "MSFT", 10, 25.93, "2026-04-17")
    assert realized.realized_gains()["totals"]["realized"] == pytest.approx(4230.00, abs=0.01)


def test_two_strikes_do_not_cover_each_others_shortfall(ledger):
    option_with_contract("buy", "MSFT", 10, 21.75, "2026-04-23", "MSFT 8/21/2026 Call $450.00")
    option_with_contract("sell", "MSFT", 10, 47.75, "2026-07-30", "MSFT 12/18/2026 Call $430.00")
    totals = realized.realized_gains()["totals"]
    assert totals["realized"] == 0, "one strike covered another"
    assert totals["unmatched_proceeds"] == pytest.approx(47_750.0)

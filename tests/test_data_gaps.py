"""Gaps in the data, each paired with the thing that would close it.

Serin could already say a figure was estimated. It could not say why, or what
to do, so three honest labels in a row read as noise and one reader concluded
the numbers were simply wrong. A caveat nobody can act on is an apology.

Two properties matter more than the detection itself: every actionable gap
carries an action, and a complete portfolio produces silence. A banner that is
always present is a banner nobody reads on the day it counts.
"""

from __future__ import annotations

import pytest
from backend import data_gaps, db
from backend.models import PositionIn, TransactionIn


@pytest.fixture
def book(tmp_path):
    db.set_db_path(tmp_path / "gaps.db")
    db.init_db()
    return db


def hold(symbol, broker="robinhood", qty=100, price=100.0, source="snaptrade",
         asset_type="stock"):
    created = db.create_position(PositionIn(
        symbol=symbol, name=symbol, broker=broker, asset_type=asset_type,
        quantity=qty, average_cost=price, current_price=price,
    ))
    with db.connect() as conn:
        conn.execute("UPDATE positions SET source=? WHERE id=?", (source, created.id))
    return created


def trade(action, symbol, qty, price, when="2026-03-01", broker="robinhood"):
    return db.create_transaction(TransactionIn(
        symbol=symbol, broker=broker, action=action,
        quantity=qty, price=price, occurred_at=when,
    ))


def priced(monkeypatch, symbols):
    """Pretend the provider has closes for exactly these symbols."""
    from backend import prices

    monkeypatch.setattr(
        prices, "fetch_price_history",
        lambda period="1y", refresh=False: {
            "history": {s: {"dates": ["2026-01-02", "2026-08-29"],
                            "closes": [1.0, 2.0]} for s in symbols}
        },
    )


def codes(result):
    return [g["code"] for g in result["gaps"]]


# --- the whole point: silence when there is nothing to do ------------------


def test_a_complete_book_reports_no_gaps(book, monkeypatch):
    priced(monkeypatch, ["TQQQ"])
    hold("TQQQ")
    trade("buy", "TQQQ", 100, 50.0)
    result = data_gaps.data_gaps()
    assert result["complete"] is True
    assert result["gaps"] == []


def test_an_empty_portfolio_says_nothing(book, monkeypatch):
    priced(monkeypatch, [])
    assert data_gaps.data_gaps()["gaps"] == []


# --- a broker with no ledger at all ----------------------------------------


def test_a_broker_with_no_trades_is_named_with_its_value(book, monkeypatch):
    priced(monkeypatch, ["TQQQ", "AAPL"])
    hold("TQQQ", broker="robinhood")
    trade("buy", "TQQQ", 100, 50.0)
    hold("AAPL", broker="etrade", qty=88, price=300.0)

    gap = next(g for g in data_gaps.data_gaps()["gaps"]
               if g["code"] == "broker_without_ledger")
    assert gap["broker"] == "etrade"
    assert gap["symbols"] == ["AAPL"]
    assert gap["value"] == pytest.approx(26400.0)
    assert gap["action"], "a high-severity gap with no action is just an apology"


def test_a_broker_with_partial_history_is_not_reported_as_having_none(book, monkeypatch):
    """One traded symbol means the ledger reaches this broker. The remaining
    holes are the sales-without-purchase case, not this one."""
    priced(monkeypatch, ["TQQQ", "HOOD"])
    hold("TQQQ", broker="robinhood")
    hold("HOOD", broker="robinhood")
    trade("buy", "TQQQ", 100, 50.0)
    assert "broker_without_ledger" not in codes(data_gaps.data_gaps())


def test_each_broker_is_reported_separately(book, monkeypatch):
    """Two accounts are two imports. Merging them into one line would hide
    which of them still needs doing."""
    priced(monkeypatch, ["TQQQ", "AAPL", "FBALX"])
    hold("TQQQ", broker="robinhood")
    trade("buy", "TQQQ", 100, 50.0)
    hold("AAPL", broker="etrade")
    hold("FBALX", broker="fidelity")
    brokers = [g["broker"] for g in data_gaps.data_gaps()["gaps"]
               if g["code"] == "broker_without_ledger"]
    assert brokers == ["etrade", "fidelity"]


def test_a_hand_entered_account_is_told_to_import_a_statement(book, monkeypatch):
    """It has no brokerage connection, so "Import transaction history" would
    point at a button that cannot help it."""
    priced(monkeypatch, ["VTI"])
    hold("VTI", broker="vanguard", source="manual")
    gap = next(g for g in data_gaps.data_gaps()["gaps"]
               if g["code"] == "broker_without_ledger")
    assert "statement" in gap["action"].lower()


def test_cash_rows_do_not_make_a_broker_look_untraded(book, monkeypatch):
    priced(monkeypatch, ["TQQQ"])
    hold("TQQQ", broker="robinhood")
    hold("CASH", broker="robinhood", asset_type="cash")
    trade("buy", "TQQQ", 100, 50.0)
    assert "broker_without_ledger" not in codes(data_gaps.data_gaps())


# --- sales whose purchase predates the ledger ------------------------------


def test_sales_without_a_purchase_name_the_date_to_import_before(book, monkeypatch):
    """The action has to be specific. "Import more history" is not something a
    person can carry out; "import statements before 2026-01-02" is."""
    priced(monkeypatch, ["AFRM"])
    hold("AFRM", qty=0)
    trade("sell", "AFRM", 519, 84.0, when="2026-07-09")

    gap = next(g for g in data_gaps.data_gaps()["gaps"]
               if g["code"] == "sales_without_purchase")
    assert gap["symbols"] == ["AFRM"]
    assert gap["value"] == pytest.approx(43596.0)
    assert gap["since"] == "2026-07-09"
    assert "2026-07-09" in gap["action"]


def test_a_matched_round_trip_raises_nothing(book, monkeypatch):
    priced(monkeypatch, ["TQQQ"])
    hold("TQQQ", qty=0)
    trade("buy", "TQQQ", 100, 50.0, when="2026-01-05")
    trade("sell", "TQQQ", 100, 70.0, when="2026-06-05")
    assert "sales_without_purchase" not in codes(data_gaps.data_gaps())


def test_only_the_unmatched_part_of_an_oversized_sale_counts(book, monkeypatch):
    priced(monkeypatch, ["TQQQ"])
    hold("TQQQ", qty=0)
    trade("buy", "TQQQ", 40, 10.0, when="2026-01-05")
    trade("sell", "TQQQ", 100, 20.0, when="2026-06-05")
    gap = next(g for g in data_gaps.data_gaps()["gaps"]
               if g["code"] == "sales_without_purchase")
    assert gap["value"] == pytest.approx(1200.0)   # the 60 uncovered shares


# --- holdings the provider cannot price ------------------------------------


def test_an_unpriceable_holding_is_explained_not_actioned(book, monkeypatch):
    """A blank period card with no explanation reads as a broken app. But
    nothing the customer imports will conjure closes that do not exist."""
    priced(monkeypatch, ["TQQQ"])
    hold("TQQQ")
    trade("buy", "TQQQ", 100, 50.0)
    hold("FBALX", broker="robinhood", qty=678, price=35.0)
    trade("buy", "FBALX", 678, 35.0)

    gap = next(g for g in data_gaps.data_gaps()["gaps"]
               if g["code"] == "no_price_history")
    assert gap["symbols"] == ["FBALX"]
    assert gap["severity"] == "info"
    assert gap["action"] == "", "there is no action; offering one would waste the reader's time"


# --- ordering and the summary figure ---------------------------------------


def test_high_severity_gaps_come_before_notes(book, monkeypatch):
    priced(monkeypatch, ["TQQQ"])
    hold("TQQQ", broker="robinhood")
    trade("buy", "TQQQ", 100, 50.0)
    hold("AAPL", broker="etrade")
    hold("FBALX", broker="robinhood")
    trade("buy", "FBALX", 1, 1.0)
    severities = [g["severity"] for g in data_gaps.data_gaps()["gaps"]]
    assert severities.index("high") < severities.index("info")


def test_the_headline_figure_counts_only_actionable_value(book, monkeypatch):
    """An unpriceable fund is not money the reader can recover by importing
    anything, so counting it would overstate what acting is worth."""
    priced(monkeypatch, ["TQQQ"])
    hold("TQQQ", broker="robinhood")
    trade("buy", "TQQQ", 100, 50.0)
    hold("AAPL", broker="etrade", qty=10, price=100.0)     # 1,000, actionable
    hold("FBALX", broker="robinhood", qty=100, price=50.0)  # unpriceable, not
    trade("buy", "FBALX", 100, 50.0)
    assert data_gaps.data_gaps()["value_affected"] == pytest.approx(1000.0)


def test_a_provider_outage_does_not_take_the_whole_report_down(book, monkeypatch):
    """Price history needs the network; the ledger gaps do not. Losing the
    actionable half because the optional half failed would be the wrong trade."""
    from backend import prices

    def boom(*args, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(prices, "fetch_price_history", boom)
    hold("AAPL", broker="etrade")
    result = data_gaps.data_gaps()
    assert "broker_without_ledger" in codes(result)


def test_the_wording_reads_correctly_for_a_single_holding(book, monkeypatch):
    """This panel exists to be read carefully at a moment of doubt. "1 holdings
    worth 24141" undermines it before the sentence finishes."""
    priced(monkeypatch, ["TQQQ", "FBALX"])
    hold("TQQQ", broker="robinhood")
    trade("buy", "TQQQ", 100, 50.0)
    hold("FBALX", broker="fidelity", qty=678, price=35.62)

    gap = next(g for g in data_gaps.data_gaps()["gaps"]
               if g["code"] == "broker_without_ledger")
    assert "1 holding worth $24,150" in gap["detail"], gap["detail"]
    assert "1 holdings" not in gap["detail"]
    assert "is assumed" in gap["detail"]


def test_money_in_the_detail_carries_a_currency_symbol(book, monkeypatch):
    priced(monkeypatch, ["AFRM"])
    hold("AFRM", qty=0)
    trade("sell", "AFRM", 519, 84.0, when="2026-07-09")
    gap = next(g for g in data_gaps.data_gaps()["gaps"]
               if g["code"] == "sales_without_purchase")
    assert "$43,596" in gap["detail"]


# --- one date per account --------------------------------------------------


def test_each_account_is_dated_to_its_own_ledger(book, monkeypatch):
    """A single global "history begins on 2016-06-09" is wrong for every other
    broker. E*Trade's activity feed reached back only to 2024-09-30, and its
    uncovered sales need statements from before *that* — telling the reader to
    find 2016 statements sends them hunting for something that would not help."""
    priced(monkeypatch, ["TQQQ", "AMD"])
    hold("TQQQ", qty=0, broker="robinhood")
    trade("buy", "TQQQ", 10, 10.0, when="2016-06-09")
    trade("sell", "TQQQ", 60, 20.0, when="2026-05-01")          # 50 uncovered
    hold("AMD", qty=0, broker="etrade")
    trade("buy", "AMD", 1, 100.0, when="2024-09-30", broker="etrade")
    trade("sell", "AMD", 101, 237.0, when="2025-11-06", broker="etrade")

    gaps = [g for g in data_gaps.data_gaps()["gaps"]
            if g["code"] == "sales_without_purchase"]
    by_broker = {g["broker"]: g for g in gaps}
    assert set(by_broker) == {"robinhood", "etrade"}
    assert by_broker["etrade"]["since"] == "2024-09-30"
    assert "2024-09-30" in by_broker["etrade"]["action"]
    assert by_broker["robinhood"]["since"] == "2016-06-09"
    assert "2016-06-09" not in by_broker["etrade"]["detail"]


def test_the_largest_uncovered_account_is_reported_first(book, monkeypatch):
    priced(monkeypatch, ["TQQQ", "AMD"])
    hold("TQQQ", qty=0, broker="robinhood")
    trade("sell", "TQQQ", 1, 10.0, when="2026-05-01")
    hold("AMD", qty=0, broker="etrade")
    trade("sell", "AMD", 500, 237.0, when="2025-11-06", broker="etrade")
    gaps = [g for g in data_gaps.data_gaps()["gaps"]
            if g["code"] == "sales_without_purchase"]
    assert gaps[0]["broker"] == "etrade"


def test_an_account_that_adds_up_raises_no_gap_of_its_own(book, monkeypatch):
    priced(monkeypatch, ["TQQQ", "AMD"])
    hold("TQQQ", qty=0, broker="robinhood")
    trade("sell", "TQQQ", 60, 20.0, when="2026-05-01")
    hold("AMD", qty=0, broker="etrade")
    trade("buy", "AMD", 100, 100.0, when="2024-09-30", broker="etrade")
    trade("sell", "AMD", 100, 237.0, when="2025-11-06", broker="etrade")
    brokers = [g["broker"] for g in data_gaps.data_gaps()["gaps"]
               if g["code"] == "sales_without_purchase"]
    assert brokers == ["robinhood"]


def test_an_uncovered_sale_is_attributed_to_the_account_that_made_it(book, monkeypatch):
    """Gap detection matches backend.realized: a lot lives in the account that
    bought it. Pooling by symbol understated this gap by exactly the amount it
    overstated realized gains."""
    priced(monkeypatch, ["GOOG"])
    hold("GOOG", qty=0, broker="etrade")
    trade("buy", "GOOG", 100, 50.0, when="2016-06-09", broker="robinhood")
    trade("sell", "GOOG", 100, 250.0, when="2026-01-02", broker="etrade")

    gap = next(g for g in data_gaps.data_gaps()["gaps"]
               if g["code"] == "sales_without_purchase")
    assert gap["broker"] == "etrade"
    assert gap["value"] == pytest.approx(25_000.0)


# --- shares that arrived rather than being bought --------------------------
#
# INTU vested in an E*Trade stock plan account and was moved into the linked
# brokerage account before being sold: 891.96 sold, 696.835 arrived by
# transfer, 195.014 predating the feed. Reporting all of it as "bought before
# your history begins on 2024-09-30" sends someone hunting for statements that
# cannot contain shares which were never bought in that account.


def test_a_transferred_in_holding_is_not_blamed_on_a_missing_statement(book, monkeypatch):
    priced(monkeypatch, ["INTU"])
    hold("INTU", qty=0, broker="etrade")
    trade("transfer", "INTU", 100, 0.0, when="2026-07-02", broker="etrade")
    trade("sell", "INTU", 100, 600.0, when="2026-08-01", broker="etrade")

    codes_seen = codes(data_gaps.data_gaps())
    assert "transferred_without_cost" in codes_seen
    assert "sales_without_purchase" not in codes_seen, \
        "a transferred share was reported as a missing statement"

    gap = next(g for g in data_gaps.data_gaps()["gaps"]
               if g["code"] == "transferred_without_cost")
    assert gap["broker"] == "etrade"
    assert gap["symbols"] == ["INTU"]
    assert gap["value"] == pytest.approx(60_000.0)
    assert "cost" in gap["action"].lower()


def test_a_hole_is_split_between_the_transfer_and_the_missing_history(book, monkeypatch):
    """The real shape of it: part explained by a transfer, part genuinely
    older than the ledger. Each half needs a different thing done."""
    priced(monkeypatch, ["INTU"])
    hold("INTU", qty=0, broker="etrade")
    trade("transfer", "INTU", 70, 0.0, when="2026-07-02", broker="etrade")
    trade("sell", "INTU", 100, 600.0, when="2026-08-01", broker="etrade")

    gaps = {g["code"]: g for g in data_gaps.data_gaps()["gaps"]}
    assert gaps["transferred_without_cost"]["value"] == pytest.approx(42_000.0)
    assert gaps["sales_without_purchase"]["value"] == pytest.approx(18_000.0)


def test_a_transfer_does_not_excuse_a_sale_of_a_different_symbol(book, monkeypatch):
    priced(monkeypatch, ["INTU", "AVGO"])
    hold("AVGO", qty=0, broker="etrade")
    trade("transfer", "INTU", 100, 0.0, when="2026-07-02", broker="etrade")
    trade("sell", "AVGO", 100, 300.0, when="2026-08-01", broker="etrade")
    gaps = {g["code"]: g for g in data_gaps.data_gaps()["gaps"]}
    assert "transferred_without_cost" not in gaps
    assert gaps["sales_without_purchase"]["value"] == pytest.approx(30_000.0)


def test_a_transfer_into_one_account_does_not_cover_a_sale_in_another(book, monkeypatch):
    priced(monkeypatch, ["INTU"])
    hold("INTU", qty=0, broker="robinhood")
    trade("transfer", "INTU", 100, 0.0, when="2026-07-02", broker="etrade")
    trade("sell", "INTU", 100, 600.0, when="2026-08-01", broker="robinhood")
    gaps = {g["code"]: g for g in data_gaps.data_gaps()["gaps"]}
    assert "transferred_without_cost" not in gaps


def test_a_transfer_covering_a_normal_purchase_changes_nothing(book, monkeypatch):
    """A transfer alongside a fully covered sale must not invent a gap."""
    priced(monkeypatch, ["INTU"])
    hold("INTU", qty=0, broker="etrade")
    trade("buy", "INTU", 100, 500.0, when="2026-01-02", broker="etrade")
    trade("transfer", "INTU", 50, 0.0, when="2026-07-02", broker="etrade")
    trade("sell", "INTU", 100, 600.0, when="2026-08-01", broker="etrade")
    assert "transferred_without_cost" not in codes(data_gaps.data_gaps())


def test_the_transfer_gap_carries_the_rows_that_need_a_price(book, monkeypatch):
    """The action is "record what these shares cost", so everything except the
    cost has to travel with it — otherwise the button lands the reader on a
    tab with 984 rows and no indication which ones it meant."""
    priced(monkeypatch, ["INTU"])
    hold("INTU", qty=0, broker="etrade")
    trade("transfer", "INTU", 30, 0.0, when="2026-07-02", broker="etrade")
    trade("transfer", "INTU", 40, 0.0, when="2026-07-02", broker="etrade")
    trade("transfer", "INTU", 30, 0.0, when="2026-08-03", broker="etrade")
    trade("sell", "INTU", 100, 600.0, when="2026-08-10", broker="etrade")

    gap = next(g for g in data_gaps.data_gaps()["gaps"]
               if g["code"] == "transferred_without_cost")
    # Same-day allocations share a vest price, so they arrive as one row.
    assert gap["lots"] == [
        {"symbol": "INTU", "broker": "etrade", "date": "2026-07-02", "quantity": 70.0},
        {"symbol": "INTU", "broker": "etrade", "date": "2026-08-03", "quantity": 30.0},
    ]


def test_the_lots_do_not_leak_across_accounts(book, monkeypatch):
    priced(monkeypatch, ["INTU"])
    hold("INTU", qty=0, broker="etrade")
    trade("transfer", "INTU", 50, 0.0, when="2026-07-02", broker="etrade")
    trade("transfer", "INTU", 50, 0.0, when="2026-07-02", broker="robinhood")
    trade("sell", "INTU", 50, 600.0, when="2026-08-10", broker="etrade")
    gap = next(g for g in data_gaps.data_gaps()["gaps"]
               if g["code"] == "transferred_without_cost")
    assert [lot["broker"] for lot in gap["lots"]] == ["etrade"]


# --- putting a reminder away -----------------------------------------------


def test_a_dismissed_gap_stays_in_the_list_but_leaves_the_active_set(book, monkeypatch):
    """Dismissing files it in the action hub; it does not delete it. The work
    is still outstanding and the number still wrong."""
    priced(monkeypatch, ["TQQQ", "AAPL"])
    hold("TQQQ", broker="robinhood")
    trade("buy", "TQQQ", 100, 50.0)
    hold("AAPL", broker="etrade", qty=88, price=300.0)

    before = data_gaps.data_gaps()
    gap = next(g for g in before["gaps"] if g["code"] == "broker_without_ledger")
    assert gap["id"] == "broker_without_ledger:etrade"
    assert gap["dismissed"] is False

    data_gaps.set_dismissed(gap["id"], True)
    after = data_gaps.data_gaps()
    assert [g["id"] for g in after["gaps"]] == [g["id"] for g in before["gaps"]]
    assert gap["id"] not in [g["id"] for g in after["active"]]
    assert after["dismissed_count"] == 1
    assert after["value_dismissed"] == pytest.approx(26_400.0)


def test_the_panel_goes_quiet_once_everything_is_dismissed(book, monkeypatch):
    """complete drives whether the panel renders at all, so it has to answer
    about what is still asking to be seen."""
    priced(monkeypatch, ["AAPL"])
    hold("AAPL", broker="etrade")
    for gap in data_gaps.data_gaps()["gaps"]:
        data_gaps.set_dismissed(gap["id"], True)
    result = data_gaps.data_gaps()
    assert result["complete"] is True
    assert result["active"] == []
    assert result["gaps"], "the gaps themselves must survive for the hub"


def test_a_dismissal_can_be_undone(book, monkeypatch):
    priced(monkeypatch, ["AAPL"])
    hold("AAPL", broker="etrade")
    gap_id = data_gaps.data_gaps()["gaps"][0]["id"]
    data_gaps.set_dismissed(gap_id, True)
    data_gaps.set_dismissed(gap_id, False)
    assert data_gaps.data_gaps()["active"], "restoring did not bring it back"


def test_the_id_survives_the_value_changing(book, monkeypatch):
    """A gap is identified by what it is, not by how big it is — otherwise
    dismissing it would un-dismiss itself the next time a price moved."""
    priced(monkeypatch, ["AAPL"])
    first = hold("AAPL", broker="etrade", qty=10, price=100.0)
    before = data_gaps.data_gaps()["gaps"][0]["id"]
    with db.connect() as conn:
        conn.execute("UPDATE positions SET current_price=? WHERE id=?", (900.0, first.id))
    assert data_gaps.data_gaps()["gaps"][0]["id"] == before


def test_a_dismissal_for_one_account_leaves_the_other_showing(book, monkeypatch):
    priced(monkeypatch, ["AAPL", "FBALX"])
    hold("AAPL", broker="etrade")
    hold("FBALX", broker="fidelity")
    data_gaps.set_dismissed("broker_without_ledger:etrade", True)
    active = [g["id"] for g in data_gaps.data_gaps()["active"]]
    assert "broker_without_ledger:fidelity" in active
    assert "broker_without_ledger:etrade" not in active

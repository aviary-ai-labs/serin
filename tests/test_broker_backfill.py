"""Importing broker activity history into the ledger.

Two faults sat here at once, and the second was only reachable once the first
was fixed:

1. The call didn't exist. SDK 13 renamed the API group and moved activities
   under the account — `transactions_and_reporting.get_activities` raised
   AttributeError before any request went out, so the button failed with a
   generic 502 that read as a SnapTrade outage. Same shape as the SDK 13
   constructor change that cost months.

2. It would have doubled every trade already imported from a broker CSV. The
   database's unique index catches a repeated backfill, because those carry
   the same `snaptrade:` reference — but it cannot see that the same real
   trade arrived from a statement under a different fingerprint. A ledger with
   201 CSV rows, 166 of them buys and sells inside the backfill window, would
   have gained 166 phantom trades, and a doubled trade corrupts share counts,
   TWR, MWR and coverage together.
"""

from __future__ import annotations

import pytest
from backend import db, snaptrade
from backend.models import PositionIn, TransactionIn


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    db.set_db_path(tmp_path / "backfill.db")
    db.init_db()
    monkeypatch.setattr(
        snaptrade, "get_stored_user", lambda: {"userId": "u1", "userSecret": "s1"}
    )
    return db


def hold(symbol, qty=100.0, broker="robinhood", price=50.0):
    """A position for the repair to reconcile the ledger against."""
    return db.create_position(PositionIn(
        symbol=symbol, name=symbol, broker=broker, asset_type="stock",
        quantity=qty, average_cost=price, current_price=price,
    ))


def activity(activity_id, type_="BUY", symbol="TQQQ", units=10, price=50.0,
             date="2026-03-01", fee=0.0):
    return {
        "id": activity_id, "type": type_, "units": units, "price": price,
        "amount": units * price, "fee": fee, "trade_date": date,
        "institution": "Robinhood", "currency": {"code": "USD"},
        "symbol": {"symbol": symbol, "raw_symbol": symbol},
    }


class FakeAccounts:
    """Stands in for the SDK's account_information group."""

    def __init__(self, pages):
        self.pages = pages          # list of pages, returned in order
        self.calls = []

    def get_account_activities(self, **kwargs):
        self.calls.append(kwargs)
        page = self.pages.pop(0) if self.pages else []
        return type("R", (), {"body": page})()


def install(monkeypatch, pages, accounts=None):
    accounts = accounts or [{"id": "acct-1", "institution": "Robinhood"}]
    group = FakeAccounts(list(pages))
    monkeypatch.setattr(snaptrade, "list_accounts", lambda: accounts)
    monkeypatch.setattr(
        snaptrade, "_get_client",
        lambda: type("C", (), {"account_information": group})(),
    )
    return group


# --- the call that did not exist -------------------------------------------


def test_activities_are_fetched_per_account_from_account_information(ledger, monkeypatch):
    """The endpoint lives under the account in SDK 13, and takes an account_id.
    Calling it user-wide is what raised AttributeError."""
    group = install(monkeypatch, [[activity("a1")]],
                    accounts=[{"id": "acct-1"}, {"id": "acct-2"}])
    snaptrade.backfill_transactions(days=365)
    assert [c["account_id"] for c in group.calls] == ["acct-1", "acct-2"]
    for call in group.calls:
        assert call["user_id"] == "u1" and call["user_secret"] == "s1"


def test_every_account_is_visited_not_just_the_first(ledger, monkeypatch):
    """Four Robinhood accounts is the ordinary case — individual, Roth,
    traditional, crypto. Stopping after one silently loses three."""
    install(monkeypatch,
            [[activity("a1")], [activity("a2")], [activity("a3")], [activity("a4")]],
            accounts=[{"id": f"acct-{i}"} for i in range(4)])
    result = snaptrade.backfill_transactions(days=365)
    assert result["imported"] == 4


def test_pages_are_walked_until_a_short_one(ledger, monkeypatch):
    """A page caps at 1000. A year of an active trader's fills exceeds that,
    and reading only the first page loses the rest without saying so."""
    full = [activity(f"a{i}", units=i + 1) for i in range(snaptrade._ACTIVITY_PAGE)]
    group = install(monkeypatch, [full, [activity("tail")]])
    result = snaptrade.backfill_transactions(days=365)
    assert result["imported"] == snaptrade._ACTIVITY_PAGE + 1
    assert [c["offset"] for c in group.calls] == [0, snaptrade._ACTIVITY_PAGE]


def test_a_short_first_page_costs_only_one_call(ledger, monkeypatch):
    """Activities are rate limited per account; a needless second call is a
    real cost, not just tidiness."""
    group = install(monkeypatch, [[activity("a1")]])
    snaptrade.backfill_transactions(days=365)
    assert len(group.calls) == 1


# --- the duplicate that would have doubled a real ledger --------------------


def test_a_trade_already_imported_from_a_csv_is_not_imported_again(ledger, monkeypatch):
    """The whole point. Same trade, different source, different fingerprint —
    nothing in the database would have caught it."""
    db.create_transaction(
        TransactionIn(symbol="TQQQ", broker="robinhood", action="buy",
                      quantity=10, price=50.0, occurred_at="2026-03-01"),
        source="import", external_id="rh:fingerprint-abc",
    )
    install(monkeypatch, [[activity("snap-1", symbol="TQQQ", units=10,
                                    price=50.0, date="2026-03-01")]])
    result = snaptrade.backfill_transactions(days=365)
    assert result["imported"] == 0
    assert result["skipped_duplicate"] == 1
    assert db.count_transactions() == 1, "the ledger gained a phantom trade"


def test_cent_rounding_between_a_statement_and_the_api_still_matches(ledger, monkeypatch):
    """A CSV rounds to the cent; the API answers in full precision. An exact
    float compare would call one fill two trades and import it twice."""
    db.create_transaction(
        TransactionIn(symbol="TQQQ", broker="robinhood", action="buy",
                      quantity=10, price=72.50, occurred_at="2026-08-20"),
        source="import", external_id="rh:xyz",
    )
    install(monkeypatch, [[activity("snap-2", symbol="TQQQ", units=10,
                                    price=72.499999, date="2026-08-20")]])
    result = snaptrade.backfill_transactions(days=365)
    assert result["skipped_duplicate"] == 1
    assert db.count_transactions() == 1


def test_a_day_the_statement_only_partly_covers_is_skipped_but_reported(
        ledger, monkeypatch):
    """The judgement call, made explicit.

    The statement carries the 500-share fill; the broker reports 500 and a 12.
    Importing the 12 would be right if the statement were merely incomplete —
    but the same shape occurs when the broker is reporting one order the
    statement recorded as many fills, and there importing anything at all
    double-counts the day. Twenty of twenty-one real overlaps were the second
    case.

    So the day is skipped, because a missed fill can be imported later while a
    double-counted one silently corrupts every reconstructed figure. The
    discrepancy is returned rather than swallowed."""
    db.create_transaction(
        TransactionIn(symbol="TQQQ", broker="robinhood", action="buy",
                      quantity=500, price=72.50, occurred_at="2026-08-20"),
        source="import", external_id="rh:the-500",
    )
    install(monkeypatch, [[
        activity("snap-3", symbol="TQQQ", units=500, price=72.50, date="2026-08-20"),
        activity("snap-4", symbol="TQQQ", units=12, price=72.50, date="2026-08-20"),
    ]])
    result = snaptrade.backfill_transactions(days=365)
    assert result["imported"] == 0
    assert result["skipped_duplicate"] == 2
    assert result["disagreements"] == [{
        "symbol": "TQQQ", "action": "buy", "date": "2026-08-20",
        "broker": "robinhood", "statement": 500.0, "broker_shares": 512.0,
    }], "a day the two sources disagree about must not be skipped silently"


def test_a_day_the_two_sources_agree_on_raises_no_discrepancy(ledger, monkeypatch):
    db.create_transaction(
        TransactionIn(symbol="TQQQ", broker="robinhood", action="buy",
                      quantity=500, price=72.50, occurred_at="2026-08-20"),
        source="import", external_id="rh:the-500",
    )
    install(monkeypatch, [[
        activity("snap-3", symbol="TQQQ", units=500, price=72.4983, date="2026-08-20"),
    ]])
    assert snaptrade.backfill_transactions(days=365)["disagreements"] == []


def test_a_genuinely_new_trade_still_imports_alongside_a_duplicate(ledger, monkeypatch):
    db.create_transaction(
        TransactionIn(symbol="TQQQ", broker="robinhood", action="buy",
                      quantity=10, price=50.0, occurred_at="2026-03-01"),
        source="import", external_id="rh:abc",
    )
    install(monkeypatch, [[
        activity("snap-5", symbol="TQQQ", units=10, price=50.0, date="2026-03-01"),
        activity("snap-6", symbol="HOOD", units=5, price=117.0, date="2026-04-01"),
    ]])
    result = snaptrade.backfill_transactions(days=365)
    assert result["imported"] == 1 and result["skipped_duplicate"] == 1
    assert {t.symbol for t in db.list_transactions()} == {"TQQQ", "HOOD"}


def test_rerunning_the_backfill_imports_nothing_new(ledger, monkeypatch):
    """Idempotence on its own reference, which is the case the unique index
    already covered — pinned so the new dedupe cannot mask a regression."""
    install(monkeypatch, [[activity("snap-7")]])
    first = snaptrade.backfill_transactions(days=365)
    install(monkeypatch, [[activity("snap-7")]])
    second = snaptrade.backfill_transactions(days=365)
    assert first["imported"] == 1
    assert second["imported"] == 0
    assert db.count_transactions() == 1


def test_an_unmappable_activity_type_is_reported_not_guessed(ledger, monkeypatch):
    install(monkeypatch, [[activity("snap-8", type_="MYSTERY")]])
    result = snaptrade.backfill_transactions(days=365)
    assert result["imported"] == 0 and result["skipped_unknown"] == 1


def test_the_response_names_every_outcome_the_page_shows(ledger, monkeypatch):
    install(monkeypatch, [[activity("snap-9")]])
    result = snaptrade.backfill_transactions(days=365)
    for field in ("imported", "skipped_existing", "skipped_duplicate", "skipped_unknown"):
        assert field in result, f"the page reads {field} and would render undefined"


def test_a_rerun_reports_already_on_record_not_a_statement_collision(ledger, monkeypatch):
    """The two skip reasons mean different things to whoever reads them.

    A repeat backfill is caught by the unique index on the snaptrade:
    reference and is ordinary. A statement collision means two sources
    described the same trade — which is the fact worth surfacing. If the
    content check ran against this backfill's own rows it would relabel every
    re-run as a collision and hide whether anything really overlapped."""
    install(monkeypatch, [[activity("snap-x")]])
    snaptrade.backfill_transactions(days=365)
    install(monkeypatch, [[activity("snap-x")]])
    second = snaptrade.backfill_transactions(days=365)
    assert second["skipped_existing"] == 1
    assert second["skipped_duplicate"] == 0, "a re-run was mislabelled a statement collision"


# --- the window we ask SnapTrade for ---------------------------------------
# Asking for 365 days was our own limit, not the vendor's: the activities
# endpoint returns all historical transactions for an account and treats the
# dates as optional filters. The cost of that cap was borne by exactly the
# customer we most want to convince — someone who signs up mid-year, connects a
# broker they have held for years, and compares our return against theirs.
# Everything they bought more than a year ago and sold this year arrived as a
# sale with no purchase on record, and a sale with no cost basis cannot be
# counted as a gain at all.


def test_no_window_is_requested_by_default(ledger, monkeypatch):
    group = install(monkeypatch, [[activity("a1")]])
    snaptrade.backfill_transactions()
    assert "start_date" not in group.calls[0], (
        "a start_date was sent, so the account's earlier history was cut off"
    )
    assert "end_date" in group.calls[0], "the end of the window should still be bounded"


def test_an_explicit_window_is_still_honoured(ledger, monkeypatch):
    """Kept so a caller can deliberately re-pull a narrow range."""
    group = install(monkeypatch, [[activity("a1")]])
    snaptrade.backfill_transactions(days=30)
    assert "start_date" in group.calls[0]


def test_the_response_reports_an_open_window_as_open(ledger, monkeypatch):
    install(monkeypatch, [[activity("a1")]])
    result = snaptrade.backfill_transactions()
    assert result["window_days"] is None
    assert result["from"] == "", "an unbounded window should not invent a start date"


# --- the envelope SnapTrade actually returns -------------------------------
# `get_account_activities` answers {"data": [...], "pagination": {...}}. The
# unwrapper handled a "results" envelope and, for anything else, fell back to
# `list(body)` — which on a dict yields its KEYS. So a response carrying 623
# trades came back as the two strings "data" and "pagination", both were
# discarded as unmappable, and the import reported success having stored
# nothing. Six accounts, 720 activities, every run.


def test_the_data_envelope_is_unwrapped(ledger):
    rows = snaptrade._response_rows(
        {"data": [activity("a1"), activity("a2")], "pagination": {"offset": 0}}
    )
    assert len(rows) == 2
    assert all(isinstance(r, dict) for r in rows), "key names came back as rows"


def test_the_older_results_envelope_still_works(ledger):
    assert len(snaptrade._response_rows({"results": [activity("a1")]})) == 1


def test_a_bare_list_is_passed_through(ledger):
    assert len(snaptrade._response_rows([activity("a1"), activity("a2")])) == 2


def test_an_unknown_envelope_yields_nothing_rather_than_key_names(ledger):
    """The failure mode that made this silent. Returning key names produced
    rows that looked real enough to iterate and always mapped to nothing."""
    rows = snaptrade._response_rows({"unexpected": [1, 2], "pagination": {}})
    assert rows == [], f"invented {rows} out of key names"


def test_an_enveloped_response_actually_imports(ledger, monkeypatch):
    """End to end, because the unwrapper being right is only useful if the
    backfill sees it."""
    class Enveloped:
        def __init__(self, pages):
            self.pages = pages
            self.calls = []

        def get_account_activities(self, **kwargs):
            self.calls.append(kwargs)
            page = self.pages.pop(0) if self.pages else []
            return type("R", (), {"body": {"data": page, "pagination": {"offset": 0}}})()

    group = Enveloped([[activity("a1"), activity("a2")]])
    monkeypatch.setattr(snaptrade, "list_accounts", lambda: [{"id": "acct-1"}])
    monkeypatch.setattr(snaptrade, "_get_client",
                        lambda: type("C", (), {"account_information": group})())
    assert snaptrade.backfill_transactions()["imported"] == 2


def test_a_day_the_statement_covers_is_not_imported_again(ledger, monkeypatch):
    """The bug this replaces. A statement lists executions and the API lists
    the order behind them: 97 separate TQQQ buys on one side, one 43,101-share
    row on the other. Comparing rows matched none of them, so every overlapping
    trade landed twice and the rewind went 15,500 shares negative against a
    holding of 2,001."""
    for _ in range(9):                       # the statement's executions
        db.create_transaction(
            TransactionIn(symbol="TQQQ", broker="robinhood", action="buy",
                          quantity=1, price=72.50, occurred_at="2026-08-20"),
            source="import", external_id="",
        )
    install(monkeypatch, [[                  # the same trading, as one order
        activity("snap-order", symbol="TQQQ", units=9, price=72.4983,
                 date="2026-08-20"),
    ]])
    result = snaptrade.backfill_transactions()
    assert result["imported"] == 0, "the same day was counted twice"
    assert result["skipped_duplicate"] == 1
    assert db.count_transactions() == 9


def test_a_day_only_the_broker_saw_still_imports(ledger, monkeypatch):
    """The overlap is small — 386 of 435 groups in the book that exposed this
    were broker-only. Skipping those would throw away the history the backfill
    exists to fetch."""
    db.create_transaction(
        TransactionIn(symbol="TQQQ", broker="robinhood", action="buy",
                      quantity=9, price=72.50, occurred_at="2026-08-20"),
        source="import", external_id="",
    )
    install(monkeypatch, [[
        activity("snap-old", symbol="TQQQ", units=500, price=40.0,
                 date="2019-03-11"),
    ]])
    result = snaptrade.backfill_transactions()
    assert result["imported"] == 1
    assert db.count_transactions() == 2


def test_the_same_symbol_on_a_different_day_is_not_confused_for_a_duplicate(
        ledger, monkeypatch):
    db.create_transaction(
        TransactionIn(symbol="TQQQ", broker="robinhood", action="buy",
                      quantity=9, price=72.50, occurred_at="2026-08-20"),
        source="import", external_id="",
    )
    install(monkeypatch, [[
        activity("s1", symbol="TQQQ", units=9, price=72.50, date="2026-08-21"),
    ]])
    assert snaptrade.backfill_transactions()["imported"] == 1


def test_the_statement_wins_inside_its_own_span_even_for_unmatched_activity(
        ledger, monkeypatch):
    """The trade-off this design accepts, stated plainly.

    A statement is one account's complete activity export for the period it
    spans, so inside that span it is the record — which is what finally caught
    the trades the two sources dated differently, settlement against
    execution. The cost is that a sale the statement genuinely omitted is
    skipped too. That is deliberate: the ledger is reconciled against the
    actual position afterwards, so a statement that was *not* complete shows
    up as an unresolved symbol rather than as silent corruption."""
    db.create_transaction(
        TransactionIn(symbol="SQQQ", broker="robinhood", action="buy",
                      quantity=250, price=20.0, occurred_at="2026-04-02"),
        source="import", external_id="",
    )
    install(monkeypatch, [[
        activity("s1", type_="SELL", symbol="SQQQ", units=251, price=21.0,
                 date="2026-04-02"),
    ]])
    assert snaptrade.backfill_transactions()["imported"] == 0


def test_activity_outside_the_statements_span_is_still_imported(
        ledger, monkeypatch):
    """Precedence is bounded by the span. Four of one book's eight remaining
    TQQQ rows predated the statement and netted to exactly zero — real
    December history, not duplication."""
    db.create_transaction(
        TransactionIn(symbol="TQQQ", broker="robinhood", action="buy",
                      quantity=250, price=20.0, occurred_at="2026-04-02"),
        source="import", external_id="",
    )
    install(monkeypatch, [[
        activity("s1", symbol="TQQQ", units=2800, price=52.25, date="2025-12-19"),
    ]])
    assert snaptrade.backfill_transactions()["imported"] == 1


def test_one_accounts_statement_does_not_suppress_anothers_trades(
        ledger, monkeypatch):
    """A Robinhood export says nothing about the Fidelity lot. Keying the span
    on symbol alone would delete real history from a second account to fix a
    duplicate in the first."""
    db.create_transaction(
        TransactionIn(symbol="TQQQ", broker="robinhood", action="buy",
                      quantity=250, price=20.0, occurred_at="2026-04-02"),
        source="import", external_id="",
    )
    install(monkeypatch, [[
        {**activity("s1", symbol="TQQQ", units=40, price=20.0,
                    date="2026-04-02"), "institution": "Fidelity"},
    ]])
    assert snaptrade.backfill_transactions()["imported"] == 1


# --- repairing a book a pre-fix backfill already corrupted -----------------


def test_repair_removes_the_broker_rows_that_duplicate_a_statement_day(ledger):
    for _ in range(9):
        db.create_transaction(
            TransactionIn(symbol="TQQQ", broker="robinhood", action="buy",
                          quantity=1, price=72.50, occurred_at="2026-08-20"),
            source="import", external_id="",
        )
    db.create_transaction(
        TransactionIn(symbol="TQQQ", broker="robinhood", action="buy",
                      quantity=9, price=72.4983, occurred_at="2026-08-20"),
        source="snaptrade", external_id="snaptrade:order-1",
    )

    preview = snaptrade.repair_duplicate_backfill(dry_run=True)
    assert preview["duplicate_rows"] == 1
    assert preview["removed"] == 0, "a dry run must not delete anything"
    assert db.count_transactions() == 10

    result = snaptrade.repair_duplicate_backfill(dry_run=False)
    assert result["removed"] == 1
    assert db.count_transactions() == 9
    assert all(t.source == "import" for t in db.list_transactions(limit=100))


def test_repair_keeps_broker_history_the_statement_never_covered(ledger):
    db.create_transaction(
        TransactionIn(symbol="TQQQ", broker="robinhood", action="buy",
                      quantity=9, price=72.50, occurred_at="2026-08-20"),
        source="import", external_id="",
    )
    db.create_transaction(
        TransactionIn(symbol="TQQQ", broker="robinhood", action="buy",
                      quantity=500, price=40.0, occurred_at="2019-03-11"),
        source="snaptrade", external_id="snaptrade:old",
    )
    snaptrade.repair_duplicate_backfill(dry_run=False)
    assert db.count_transactions() == 2


def test_repair_drops_a_duplicate_dated_outside_the_statements_span(ledger):
    """One share of SPCX bought on the 9th by the broker and the 12th by the
    statement — the same purchase, three days apart, settlement against
    execution. Pass 1 cannot see it. Pass 2 removes it only because doing so
    makes the ledger agree with the position exactly."""
    hold("SPCX", qty=1)
    db.create_transaction(
        TransactionIn(symbol="SPCX", broker="robinhood", action="buy",
                      quantity=1, price=135.0, occurred_at="2026-06-12"),
        source="import", external_id="",
    )
    db.create_transaction(
        TransactionIn(symbol="SPCX", broker="robinhood", action="buy",
                      quantity=1, price=135.0, occurred_at="2026-06-09"),
        source="snaptrade", external_id="snaptrade:spcx",
    )
    result = snaptrade.repair_duplicate_backfill(dry_run=False)
    assert result["removed"] == 1
    assert result["unresolved"] == []
    assert db.count_transactions() == 1


def test_repair_does_not_touch_history_that_already_reconciles(ledger):
    """TQQQ's four pre-statement rows netted to exactly zero against a
    statement that already matched the position. Removing them would delete
    real December history to fix nothing."""
    hold("TQQQ", qty=2001)
    db.create_transaction(
        TransactionIn(symbol="TQQQ", broker="robinhood", action="buy",
                      quantity=2001, price=50.0, occurred_at="2026-01-02"),
        source="import", external_id="",
    )
    for i, (act, qty, when) in enumerate([
        ("buy", 2800, "2025-12-19"), ("sell", 2800, "2025-12-29"),
    ]):
        db.create_transaction(
            TransactionIn(symbol="TQQQ", broker="robinhood", action=act,
                          quantity=qty, price=52.0, occurred_at=when),
            source="snaptrade", external_id=f"snaptrade:dec-{i}",
        )
    result = snaptrade.repair_duplicate_backfill(dry_run=False)
    assert result["removed"] == 0
    assert db.count_transactions() == 3


def test_repair_reports_what_it_could_not_reconcile(ledger):
    hold("TQQQ", qty=2001)
    db.create_transaction(
        TransactionIn(symbol="TQQQ", broker="robinhood", action="buy",
                      quantity=3500, price=70.0, occurred_at="2026-01-20"),
        source="import", external_id="",
    )
    result = snaptrade.repair_duplicate_backfill(dry_run=True)
    assert result["unresolved"] == [
        {"symbol": "TQQQ", "broker": "robinhood", "ledger": 3500.0, "held": 2001.0}
    ]


def test_repair_leaves_an_untouched_book_alone(ledger):
    db.create_transaction(
        TransactionIn(symbol="TQQQ", broker="robinhood", action="buy",
                      quantity=5, price=72.50, occurred_at="2026-08-20"),
        source="import", external_id="",
    )
    db.create_transaction(
        TransactionIn(symbol="HOOD", broker="robinhood", action="sell",
                      quantity=2, price=100.0, occurred_at="2026-07-01"),
        source="snaptrade", external_id="snaptrade:x",
    )
    assert snaptrade.repair_duplicate_backfill(dry_run=True)["duplicate_rows"] == 0
    snaptrade.repair_duplicate_backfill(dry_run=False)
    assert db.count_transactions() == 2


def test_interest_the_statement_already_records_is_not_added_again(
        ledger, monkeypatch):
    """Six interest credits appeared in both sources for the identical cent.
    A duplicated buy announces itself by driving share counts negative; a
    duplicated credit just quietly inflates the cash balance, which is the
    more dangerous of the two failures."""
    db.create_transaction(
        TransactionIn(symbol="", broker="robinhood", action="interest",
                      quantity=0, price=750.90, occurred_at="2026-02-27"),
        source="import", external_id="",
    )
    install(monkeypatch, [[
        activity("snap-int", type_="INTEREST", symbol="", units=0,
                 price=750.90, date="2026-02-27"),
    ]])
    result = snaptrade.backfill_transactions()
    assert result["imported"] == 0
    assert result["skipped_duplicate"] == 1
    assert db.count_transactions() == 1


def test_a_dividend_only_the_broker_reports_still_imports(ledger, monkeypatch):
    db.create_transaction(
        TransactionIn(symbol="", broker="robinhood", action="interest",
                      quantity=0, price=750.90, occurred_at="2026-02-27"),
        source="import", external_id="",
    )
    install(monkeypatch, [[
        activity("snap-div", type_="DIVIDEND", symbol="TQQQ", units=0,
                 price=42.10, date="2026-03-15"),
    ]])
    assert snaptrade.backfill_transactions()["imported"] == 1


def test_a_holding_with_no_ledger_is_not_called_a_failed_reconciliation(ledger):
    """Fourteen E*TRADE symbols with an empty ledger crowded out the two that
    genuinely did not add up. Missing history is data_gaps' story, and it
    carries an action; repeating it here buries the real finding."""
    hold("AAPL", qty=88, broker="etrade")
    hold("BABA", qty=518, broker="robinhood")
    db.create_transaction(
        TransactionIn(symbol="BABA", broker="robinhood", action="buy",
                      quantity=501, price=80.0, occurred_at="2026-02-02"),
        source="import", external_id="",
    )
    result = snaptrade.repair_duplicate_backfill(dry_run=True)
    assert [u["symbol"] for u in result["unresolved"]] == ["BABA"]


# --- the same trade, dated either side of a settlement lag -----------------


def test_a_sale_redated_by_settlement_is_removed(ledger):
    """One AFRM sale of 519 shares at $84.00: the API reported it as a single
    order on the 8th, the statement as three fills on the 9th. Outside each
    other's day and outside the statement's one-day span, so both survived and
    $43,596 of proceeds became $87,191 of cash the account never held — a
    $49,538 step in one day of the chart with no cash flow behind it.

    No position anchors this: AFRM was sold out and removed, so reconciliation
    has nothing to compare against."""
    for qty, amount in [(48, 4031.91), (199, 16715.61), (272, 22847.47)]:
        db.create_transaction(
            TransactionIn(symbol="AFRM", broker="robinhood", action="sell",
                          quantity=qty, price=84.0, occurred_at="2026-07-09"),
            source="import", external_id="",
        )
    db.create_transaction(
        TransactionIn(symbol="AFRM", broker="robinhood", action="sell",
                      quantity=519, price=84.0, occurred_at="2026-07-08"),
        source="snaptrade", external_id="snaptrade:afrm",
    )
    result = snaptrade.repair_duplicate_backfill(dry_run=False)
    assert result["removed"] == 1
    assert db.count_transactions() == 3
    assert all(t.source == "import" for t in db.list_transactions(limit=10))


def test_a_genuine_second_sale_further_out_is_kept(ledger):
    """Bounded by the window on purpose. Two weeks apart is two sales."""
    db.create_transaction(
        TransactionIn(symbol="AFRM", broker="robinhood", action="sell",
                      quantity=519, price=84.0, occurred_at="2026-07-09"),
        source="import", external_id="",
    )
    db.create_transaction(
        TransactionIn(symbol="AFRM", broker="robinhood", action="sell",
                      quantity=519, price=84.0, occurred_at="2026-07-23"),
        source="snaptrade", external_id="snaptrade:afrm-2",
    )
    assert snaptrade.repair_duplicate_backfill(dry_run=True)["duplicate_rows"] == 0


def test_a_different_quantity_nearby_is_not_treated_as_the_same_trade(ledger):
    db.create_transaction(
        TransactionIn(symbol="AFRM", broker="robinhood", action="sell",
                      quantity=519, price=84.0, occurred_at="2026-07-09"),
        source="import", external_id="",
    )
    db.create_transaction(
        TransactionIn(symbol="AFRM", broker="robinhood", action="sell",
                      quantity=300, price=84.0, occurred_at="2026-07-08"),
        source="snaptrade", external_id="snaptrade:afrm-3",
    )
    assert snaptrade.repair_duplicate_backfill(dry_run=True)["duplicate_rows"] == 0


def test_a_matching_quantity_at_a_different_price_is_kept(ledger):
    """Same size, materially different average price: a second sale, not the
    same one re-dated."""
    db.create_transaction(
        TransactionIn(symbol="AFRM", broker="robinhood", action="sell",
                      quantity=519, price=84.0, occurred_at="2026-07-09"),
        source="import", external_id="",
    )
    db.create_transaction(
        TransactionIn(symbol="AFRM", broker="robinhood", action="sell",
                      quantity=519, price=91.5, occurred_at="2026-07-08"),
        source="snaptrade", external_id="snaptrade:afrm-4",
    )
    assert snaptrade.repair_duplicate_backfill(dry_run=True)["duplicate_rows"] == 0


def test_one_statement_day_cannot_absorb_two_broker_days(ledger):
    """Otherwise a single statement sale would excuse every nearby broker row
    of the same size, deleting real history."""
    db.create_transaction(
        TransactionIn(symbol="AFRM", broker="robinhood", action="sell",
                      quantity=519, price=84.0, occurred_at="2026-07-09"),
        source="import", external_id="",
    )
    for i, when in enumerate(["2026-07-08", "2026-07-10"]):
        db.create_transaction(
            TransactionIn(symbol="AFRM", broker="robinhood", action="sell",
                          quantity=519, price=84.0, occurred_at=when),
            source="snaptrade", external_id=f"snaptrade:afrm-{i}",
        )
    assert snaptrade.repair_duplicate_backfill(dry_run=True)["duplicate_rows"] == 1


def test_the_lag_window_does_not_cross_brokers(ledger):
    db.create_transaction(
        TransactionIn(symbol="AFRM", broker="robinhood", action="sell",
                      quantity=519, price=84.0, occurred_at="2026-07-09"),
        source="import", external_id="",
    )
    db.create_transaction(
        TransactionIn(symbol="AFRM", broker="etrade", action="sell",
                      quantity=519, price=84.0, occurred_at="2026-07-08"),
        source="snaptrade", external_id="snaptrade:afrm-et",
    )
    assert snaptrade.repair_duplicate_backfill(dry_run=True)["duplicate_rows"] == 0


def test_a_backfill_leaves_no_settlement_duplicate_behind(ledger, monkeypatch):
    """The row-by-row checks cannot see this one — recognising it needs both
    sides in hand — so the import sweeps itself afterwards. Without it the
    next sync reintroduces the same phantom cash."""
    for qty in (48, 199, 272):
        db.create_transaction(
            TransactionIn(symbol="AFRM", broker="robinhood", action="sell",
                          quantity=qty, price=84.0, occurred_at="2026-07-09"),
            source="import", external_id="",
        )
    install(monkeypatch, [[
        activity("snap-afrm", type_="SELL", symbol="AFRM", units=519,
                 price=84.0, date="2026-07-08"),
    ]])
    result = snaptrade.backfill_transactions()
    assert result["swept_duplicates"] == 1
    assert db.count_transactions() == 3, "the phantom sale survived the import"


# --- what the accounts screen is allowed to claim --------------------------


def test_only_a_real_account_number_is_shown():
    """E*Trade returns an internal identifier where a number belongs, and
    masking it produced "••••JlsQ" — which looks like data a reader could
    check against a statement, and is not."""
    assert snaptrade._mask_number("*****6905") == "••••" + "6905"
    assert snaptrade._mask_number("706512") == "••••" + "6512"
    assert snaptrade._mask_number("G2JlsQxK") == ""
    assert snaptrade._mask_number("") == ""
    assert snaptrade._mask_number(None) == ""


def test_a_full_account_number_never_leaves_the_backend():
    """Four digits tells two accounts apart, which is all the screen needs."""
    masked = snaptrade._mask_number("123456789012")
    assert masked == "••••" + "9012"
    assert "12345" not in masked

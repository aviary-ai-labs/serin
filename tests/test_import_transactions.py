"""Statement import: transactions, classification, and landing exactly once.

The acceptance case that drives most of this is "importing the same statement
twice does not duplicate transactions". Everything a return is built from —
contributions, realized gains, dividends — is a sum over this table, so a
duplicate does not merely clutter a list, it corrupts every number downstream.
"""

from __future__ import annotations

import pytest
from backend import db, smart_import
from backend.models import TransactionIn


@pytest.fixture
def store(tmp_path):
    db.set_db_path(tmp_path / "import.db")
    db.init_db()
    return db


STATEMENT = [
    {"action": "sell", "symbol": "NFLX", "quantity": 50, "price": 612.40,
     "occurred_at": "2026-02-11", "broker": "schwab", "external_id": "TX-991"},
    {"action": "dividend", "symbol": "AAPL", "price": 120.50,
     "occurred_at": "2026-02-03", "broker": "schwab", "external_id": "TX-877"},
    {"action": "deposit", "price": 5000.0,
     "occurred_at": "2026-01-15", "broker": "schwab", "external_id": "TX-100"},
]


# --- idempotency ----------------------------------------------------------


def test_the_same_statement_imported_twice_lands_once(store):
    first = smart_import.import_transactions(STATEMENT)
    second = smart_import.import_transactions(STATEMENT)
    assert first == {"inserted": 3, "skipped": 0}
    assert second == {"inserted": 0, "skipped": 3}
    assert len(db.list_transactions()) == 3


def test_rows_without_a_broker_reference_are_still_deduplicated(store):
    """Most screenshots have no transaction id. Fingerprinting the row's own
    content is what makes re-uploading the same screenshot safe."""
    rows = [{"action": "buy", "symbol": "AAPL", "quantity": 10, "price": 100.0,
             "occurred_at": "2026-01-05", "broker": "fidelity"}]
    smart_import.import_transactions(rows)
    smart_import.import_transactions(rows)
    assert len(db.list_transactions()) == 1


def test_two_genuinely_different_trades_both_land(store):
    rows = [
        {"action": "buy", "symbol": "AAPL", "quantity": 10, "price": 100.0,
         "occurred_at": "2026-01-05", "broker": "fidelity"},
        {"action": "buy", "symbol": "AAPL", "quantity": 10, "price": 101.0,
         "occurred_at": "2026-01-05", "broker": "fidelity"},
    ]
    assert smart_import.import_transactions(rows)["inserted"] == 2


def test_hand_entered_rows_are_never_deduplicated(store):
    """Two identical fills in one day is ordinary. Only imports carry a
    fingerprint; typing the same thing twice must be allowed."""
    row = TransactionIn(symbol="AAPL", action="buy", quantity=1, price=100.0,
                        occurred_at="2026-01-05")
    assert db.create_transaction(row) is not None
    assert db.create_transaction(row) is not None
    assert len(db.list_transactions()) == 2


def test_a_later_statement_adds_only_its_new_rows(store):
    """The realistic case: January imported in January, then a January-plus-
    February statement uploaded in March."""
    smart_import.import_transactions(STATEMENT)
    extended = STATEMENT + [
        {"action": "fee", "price": 4.95, "occurred_at": "2026-03-01",
         "broker": "schwab", "external_id": "TX-1200"},
    ]
    result = smart_import.import_transactions(extended)
    assert result == {"inserted": 1, "skipped": 3}
    assert len(db.list_transactions()) == 4


# --- classification -------------------------------------------------------


def test_cash_amounts_are_signed_by_action(store):
    smart_import.import_transactions(STATEMENT)
    by_action = {t.action: t.amount for t in db.list_transactions()}
    assert by_action["deposit"] == 5000.0
    assert by_action["dividend"] == 120.50
    assert by_action["sell"] > 0


def test_a_deposit_is_external_and_a_transfer_is_not(store):
    """The distinction the whole return calculation rests on."""
    from backend.models import is_external_flow

    assert is_external_flow("deposit") is True
    assert is_external_flow("withdrawal") is True
    assert is_external_flow("transfer") is False
    assert is_external_flow("buy") is False


def test_broker_synonyms_are_normalized(store):
    rows = [{"action": "Sold", "symbol": "nflx", "quantity": 5, "price": 100.0,
             "date": "02/11/2026", "broker": "Charles Schwab"}]
    normalized = [smart_import._normalize_transaction(r) for r in rows]
    assert normalized[0]["action"] == "sell"
    assert normalized[0]["symbol"] == "NFLX"
    assert normalized[0]["broker"] == "charles_schwab"
    assert normalized[0]["occurred_at"] == "2026-02-11"


# --- refusing to guess ----------------------------------------------------


def test_an_undated_transaction_is_dropped(store):
    """A row with no date cannot be placed in a return series, and inventing
    one moves somebody's performance to a day that never happened."""
    assert smart_import._normalize_transaction(
        {"action": "buy", "symbol": "AAPL", "quantity": 1, "price": 100.0}
    ) is None


def test_a_trade_with_no_instrument_is_dropped(store):
    assert smart_import._normalize_transaction(
        {"action": "buy", "quantity": 1, "price": 100.0, "date": "2026-01-05"}
    ) is None


def test_an_unrecognized_action_is_dropped_rather_than_guessed(store):
    assert smart_import._normalize_transaction(
        {"action": "mystery", "symbol": "AAPL", "date": "2026-01-05"}
    ) is None


def test_import_survives_a_malformed_row(store):
    rows = STATEMENT + [{"action": "buy", "symbol": "X", "quantity": "not-a-number",
                         "price": 1, "occurred_at": "2026-01-05"}]
    result = smart_import.import_transactions(rows)
    assert result["inserted"] == 3
    assert result["skipped"] == 1


# --- the import feeds performance -----------------------------------------


def test_an_imported_statement_produces_a_coverage_verdict(store):
    """End to end: import a statement, then ask what the history is worth."""
    from backend import portfolio_history as ph

    smart_import.import_transactions(STATEMENT)
    cover = ph.coverage(db.list_positions(include_closed=True), db.list_transactions())
    assert cover["since"] == "2026-01-15"
    assert cover["external_flows"] == 1
    assert cover["trades"] == 1


# --- what a brokerage app screen actually looks like ----------------------
# Found by testing against a real Robinhood history screenshot: every row was
# being dropped. The prompt asks for clean values; the model returns what is
# on the screen, and the screen says "limit buy" on "Aug 19, 2026".

TQQQ_HISTORY = [
    {"action": "limit buy", "symbol": "TQQQ", "quantity": 2000, "price": 72.50,
     "occurred_at": "Aug 19, 2026", "broker": "robinhood"},
    {"action": "market sell", "symbol": "TQQQ", "quantity": 1500, "price": 75.09,
     "occurred_at": "Aug 12, 2026", "broker": "robinhood"},
    {"action": "limit buy", "symbol": "TQQQ", "quantity": 1500, "price": 65.00,
     "occurred_at": "Jul 30, 2026", "broker": "robinhood"},
]


def test_a_real_brokerage_history_screen_imports(store):
    """The whole screenshot, end to end."""
    result = smart_import.import_transactions(TQQQ_HISTORY)
    assert result["inserted"] == 3, "rows from a real history screen were dropped"
    actions = sorted(t.action for t in db.list_transactions())
    assert actions == ["buy", "buy", "sell"]


@pytest.mark.parametrize("phrasing,expected", [
    ("limit buy", "buy"), ("market sell", "sell"), ("stop-limit sell", "sell"),
    ("Limit Buy", "buy"), ("market buy", "buy"), ("ACH deposit", "deposit"),
])
def test_order_types_are_stripped_from_the_action(phrasing, expected):
    """Brokerages name the order type as well as the direction. Matching the
    whole phrase dropped every row on the screen."""
    row = smart_import._normalize_transaction(
        {"action": phrasing, "symbol": "TQQQ", "quantity": 1, "price": 1,
         "occurred_at": "2026-01-05"}
    )
    assert row is not None, f"{phrasing!r} was rejected"
    assert row["action"] == expected


@pytest.mark.parametrize("written,iso", [
    ("Aug 19, 2026", "2026-08-19"), ("August 3 2026", "2026-08-03"),
    ("19 Aug 2026", "2026-08-19"), ("08/12/2026", "2026-08-12"),
    ("2026-07-30", "2026-07-30"),
])
def test_the_date_formats_brokerage_screens_print(written, iso):
    row = smart_import._normalize_transaction(
        {"action": "buy", "symbol": "TQQQ", "quantity": 1, "price": 1, "occurred_at": written}
    )
    assert row is not None, f"{written!r} was rejected"
    assert row["occurred_at"] == iso


def test_an_unparseable_date_is_rejected_not_stored():
    """A tax lot with a bad date gets reviewed in a form. A transaction goes
    straight into the ledger, where an unparsed date silently misplaces it in
    every return series built from it."""
    assert smart_import._normalize_transaction(
        {"action": "buy", "symbol": "X", "quantity": 1, "price": 1,
         "occurred_at": "sometime last spring"}
    ) is None


def test_a_word_that_merely_contains_an_action_is_not_matched():
    """Scanning for action words has to be word-wise; substring matching would
    turn 'buyback' into a purchase."""
    assert smart_import._normalize_transaction(
        {"action": "buyback offer", "symbol": "X", "quantity": 1, "price": 1,
         "occurred_at": "2026-01-05"}
    ) is None


def test_the_screenshot_produces_a_sale_the_performance_engine_can_see(store):
    """The point of importing a history screen: a sale on record is what lets
    a position that is now smaller — or gone — keep its past."""
    from backend import portfolio_history as ph

    smart_import.import_transactions(TQQQ_HISTORY)
    txns = db.list_transactions()
    cover = ph.coverage([], txns)
    assert cover["trades"] == 3
    assert cover["since"] == "2026-07-30"
    sells = [t for t in txns if t.action == "sell"]
    assert len(sells) == 1
    assert sells[0].quantity == 1500

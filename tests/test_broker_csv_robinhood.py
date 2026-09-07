"""Robinhood activity-export parsing.

The property under test throughout: this path is *exact*. Smart Import's other
doors infer, and inference is fine when the input has no schema. A CSV has one,
so anything this parser is unsure about must come back as a reported hole, not
as a plausible-looking row in somebody's ledger.
"""

from __future__ import annotations

import pytest
from backend import broker_csv

HEADER = (
    '"Activity Date","Process Date","Settle Date","Instrument","Description",'
    '"Trans Code","Quantity","Price","Amount"'
)


def csv_of(*rows: str) -> str:
    return "\n".join((HEADER, *rows)) + "\n"


def by_action(result: broker_csv.ParseResult, action: str) -> list[dict]:
    return [t for t in result.transactions if t["action"] == action]


# --- detection -------------------------------------------------------------


def test_detects_a_robinhood_export_by_its_header():
    fmt = broker_csv.detect(csv_of('"8/19/2026","8/19/2026","8/20/2026","TQQQ","ProShares","Sell","50","$88.30","$4,415.00"'))
    assert fmt is not None
    assert fmt.broker == "robinhood"


def test_leaves_an_unknown_csv_for_the_model():
    """Falling through is the correct outcome, not a failure: an unrecognised
    export still has the AI path. Claiming a file this parser cannot read is
    the bad case, because it would silently import nothing."""
    other = "Date,Symbol,Shares,Cost\n2026-01-02,AAPL,10,1500\n"
    assert broker_csv.detect(other) is None
    assert broker_csv.parse(other) is None


def test_ignores_free_text_and_screenshots():
    assert broker_csv.detect("I sold 50 TQQQ on August 19th") is None
    assert broker_csv.detect("") is None


# --- trades ----------------------------------------------------------------


def test_reads_a_plain_buy():
    result = broker_csv.parse(csv_of(
        '"8/12/2026","8/12/2026","8/13/2026","TQQQ","ProShares UltraPro QQQ","Buy","50","$80.00","($4,000.00)"'
    ))
    (row,) = result.transactions
    assert row["action"] == "buy"
    assert row["symbol"] == "TQQQ"
    assert row["quantity"] == 50
    assert row["price"] == 80.0
    assert row["occurred_at"] == "2026-08-12"
    assert row["broker"] == "robinhood"


def test_recovers_the_regulatory_fee_baked_into_a_sale():
    """Robinhood charges no commission, but SEC/TAF fees come out of the
    proceeds — so quantity x price does not equal the amount banked. That gap
    is the fee, and dropping it quietly overstates the realised gain."""
    result = broker_csv.parse(csv_of(
        '"8/19/2026","8/19/2026","8/20/2026","TQQQ","ProShares UltraPro QQQ","Sell","50","$88.30","$4,414.87"'
    ))
    (row,) = result.transactions
    assert row["action"] == "sell"
    assert row["fee"] == pytest.approx(0.13, abs=0.001)


def test_refuses_to_book_an_implausible_fee():
    """A large gap means the row is not what the parser thinks it is. Booking
    it as a commission would corrupt the very cost basis it meant to refine, so
    it imports at zero fee and says so."""
    result = broker_csv.parse(csv_of(
        '"8/19/2026","8/19/2026","8/20/2026","TQQQ","ProShares","Sell","50","$88.30","$400.00"'
    ))
    (row,) = result.transactions
    assert row["fee"] == 0.0
    assert any("worth checking" in w for w in result.warnings)


def test_derives_a_missing_price_from_the_amount():
    result = broker_csv.parse(csv_of(
        '"8/12/2026","8/12/2026","8/13/2026","AAPL","Apple Inc.","Buy","10","","($2,000.00)"'
    ))
    (row,) = result.transactions
    assert row["price"] == pytest.approx(200.0)


def test_drops_a_trade_with_no_instrument_or_no_shares():
    """Either one makes the row unreplayable against a price series, and a
    zero-share 'trade' would still shift the count it is replayed against."""
    result = broker_csv.parse(csv_of(
        '"8/12/2026","8/12/2026","","","Something","Buy","10","$5.00","($50.00)"',
        '"8/12/2026","8/12/2026","","AAPL","Apple Inc.","Buy","0","$5.00","$0.00"',
    ))
    assert result.transactions == []
    assert len(result.unknown) == 2


# --- cash ------------------------------------------------------------------


def test_reads_deposits_and_withdrawals_from_the_sign_of_the_amount():
    """ACH is one code for both directions. Getting this backwards is the
    classic TWR error — a withdrawal counted as a deposit turns a loss into
    apparent skill."""
    result = broker_csv.parse(csv_of(
        '"8/15/2026","8/15/2026","","","ACH Deposit","ACH","","","$1,000.00"',
        '"8/16/2026","8/16/2026","","","ACH Withdrawal","ACH","","","($250.00)"',
    ))
    assert by_action(result, "deposit")[0]["price"] == 1000.0
    assert by_action(result, "withdrawal")[0]["price"] == 250.0


def test_interest_charged_is_a_cost_not_income():
    """The same code carries both. Margin interest booked as income would
    inflate returns by exactly twice the amount."""
    result = broker_csv.parse(csv_of(
        '"8/01/2026","8/01/2026","","","Interest Earned","INT","","","$3.21"',
        '"8/02/2026","8/02/2026","","","Margin Interest","INT","","","($9.40)"',
    ))
    assert by_action(result, "interest")[0]["price"] == 3.21
    assert by_action(result, "fee")[0]["price"] == 9.40


def test_reads_dividends_withholding_and_subscription_fees():
    result = broker_csv.parse(csv_of(
        '"7/31/2026","7/31/2026","","AAPL","Apple Inc. - Dividend","CDIV","","","$12.50"',
        '"7/31/2026","7/31/2026","","AAPL","Dividend Withholding","DTAX","","","($1.88)"',
        '"7/05/2026","7/05/2026","","","Gold Subscription Fee","GOLD","","","($5.00)"',
    ))
    assert by_action(result, "dividend")[0]["price"] == 12.50
    assert by_action(result, "tax")[0]["price"] == 1.88
    assert by_action(result, "fee")[0]["price"] == 5.00


def test_accounting_parentheses_are_negative():
    """Reading "($5.00)" as +5 flips a fee into income. Every export uses
    them somewhere."""
    assert broker_csv._money("($5.00)") == -5.0
    assert broker_csv._money("-$5.00") == -5.0
    assert broker_csv._money("$1,234.56") == pytest.approx(1234.56)
    assert broker_csv._money("") is None
    assert broker_csv._money("--") is None


# --- corporate actions and options ----------------------------------------


def test_splits_come_through_as_splits():
    result = broker_csv.parse(csv_of(
        '"6/15/2026","6/15/2026","","TQQQ","ProShares UltraPro QQQ Split","SPL","100","","$0.00"',
    ))
    (row,) = result.transactions
    assert row["action"] == "split"
    assert row["quantity"] == 100
    assert row["price"] == 0.0


def test_option_trades_are_marked_and_never_fee_inferred():
    """A contract is priced per share but moves 100x the cash, so the residual
    that means 'fee' on a stock trade means nothing here."""
    result = broker_csv.parse(csv_of(
        '"8/12/2026","8/12/2026","8/13/2026","AAPL","AAPL 8/22/2026 Call $200","BTO","2","$3.50","($700.00)"',
    ))
    (row,) = result.transactions
    assert row["asset_type"] == "option"
    assert row["action"] == "buy"
    assert row["fee"] == 0.0


# --- the honesty guarantees ------------------------------------------------


def test_an_unrecognised_code_is_reported_not_guessed():
    """The whole contract of this module. A ledger with a plausible wrong row
    is worse than one with a visible hole."""
    result = broker_csv.parse(csv_of(
        '"8/12/2026","8/12/2026","","XYZ","Something new","ZZZZ","1","$1.00","$1.00"',
    ))
    assert result.transactions == []
    assert result.unknown[0]["code"] == "ZZZZ"
    assert "ZZZZ" in broker_csv.summarise(result)


def test_an_undated_row_is_dropped_and_counted():
    """A transaction with no date cannot be placed in a return series, and
    inventing one moves performance to a day that never happened."""
    result = broker_csv.parse(csv_of(
        '"","","","AAPL","Apple Inc.","Buy","10","$100.00","($1,000.00)"',
    ))
    assert result.transactions == []
    assert any("no readable date" in w for w in result.warnings)


def test_two_digit_years_are_refused_rather_than_placed_in_a_guessed_decade():
    assert broker_csv._iso_date("8/19/26") is None
    assert broker_csv._iso_date("2026-08-19") == "2026-08-19"
    assert broker_csv._iso_date("8/19/2026") == "2026-08-19"
    assert broker_csv._iso_date("13/45/2026") is None


def test_the_same_export_parses_identically_every_time():
    """The reason this path exists instead of the model: a ledger has to be
    reproducible, and re-uploading the same file must be a no-op."""
    text = csv_of(
        '"8/19/2026","8/19/2026","8/20/2026","TQQQ","ProShares","Sell","50","$88.30","$4,414.87"',
        '"8/15/2026","8/15/2026","","","ACH Deposit","ACH","","","$1,000.00"',
    )
    first = broker_csv.parse(text).transactions
    second = broker_csv.parse(text).transactions
    assert first == second
    assert all(row["external_id"] for row in first)


def test_repeated_identical_rows_survive_as_separate_transactions():
    """Two identical recurring buys on one day are two events. Collapsing them
    would understate contributions; giving them unstable ids would double them
    on the next import."""
    row = '"8/12/2026","8/12/2026","8/13/2026","VOO","Vanguard S&P 500","Buy","1","$500.00","($500.00)"'
    result = broker_csv.parse(csv_of(row, row))
    assert len(result.transactions) == 2
    ids = [t["external_id"] for t in result.transactions]
    assert ids[0] != ids[1]
    # A longer export containing the same two rows reproduces the same ids, so
    # the overlap dedupes on re-import instead of doubling.
    later = broker_csv.parse(csv_of(
        '"8/20/2026","8/20/2026","","","ACH Deposit","ACH","","","$100.00"', row, row,
    ))
    assert [t["external_id"] for t in later.transactions if t["symbol"] == "VOO"] == ids


# --- a whole file ----------------------------------------------------------

REALISTIC = csv_of(
    '"8/19/2026","8/19/2026","8/20/2026","TQQQ","ProShares UltraPro QQQ","Sell","50","$88.30","$4,414.87"',
    '"8/15/2026","8/15/2026","","","ACH Deposit","ACH","","","$1,000.00"',
    '"8/12/2026","8/12/2026","8/13/2026","TQQQ","ProShares UltraPro QQQ","Buy","50","$80.00","($4,000.00)"',
    '"7/31/2026","7/31/2026","","AAPL","Apple Inc. - Dividend","CDIV","","","$12.50"',
    '"7/05/2026","7/05/2026","","","Gold Subscription Fee","GOLD","","","($5.00)"',
    '"6/15/2026","6/15/2026","","NVDA","NVIDIA Split","SPL","90","","$0.00"',
    '"6/01/2026","6/01/2026","","","Unknown Thing","QQQQ","","","$1.00"',
    '""," "," ","","","","","",""',
)


def test_a_realistic_export_round_trips():
    result = broker_csv.parse(REALISTIC)
    assert len(result.transactions) == 6
    assert {t["action"] for t in result.transactions} == {
        "sell", "deposit", "buy", "dividend", "fee", "split",
    }
    assert len(result.unknown) == 1
    assert result.ignored >= 1
    assert all(t["broker"] == "robinhood" for t in result.transactions)


def test_the_closed_position_a_user_could_not_enter_by_hand_comes_through():
    """The case that started this: TQQQ bought and sold, gone from the account,
    invisible to a holdings-only import, and tedious to type in by hand."""
    result = broker_csv.parse(REALISTIC)
    tqqq = [t for t in result.transactions if t["symbol"] == "TQQQ"]
    assert {t["action"] for t in tqqq} == {"buy", "sell"}
    assert sum(t["quantity"] for t in tqqq) == 100


def test_every_action_is_one_the_ledger_accepts():
    """The parser's vocabulary and TransactionIn's closed set have to agree, or
    a parsed row is silently dropped at write time."""
    from backend.models import TransactionAction

    allowed = set(TransactionAction.__args__)
    for row in broker_csv.parse(REALISTIC).transactions:
        assert row["action"] in allowed
    assert set(broker_csv._RH_SIMPLE.values()) <= allowed
    for inbound, outbound in broker_csv._RH_SIGNED.values():
        assert {inbound, outbound} <= allowed


# --- through the real route and into the ledger ----------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    from backend import db
    from backend.main import app
    from fastapi.testclient import TestClient

    db.set_db_path(tmp_path / "rh.db")
    db.init_db()
    # If the deterministic path ever stops claiming this file, the route would
    # fall through to a provider. Make that a loud failure rather than a
    # surprise bill.
    async def _boom(**_kwargs):
        raise AssertionError("a recognised broker export reached the AI extractor")

    monkeypatch.setattr("backend.smart_import.extract", _boom)
    return TestClient(app)


def post_export(client, text: str):
    return client.post(
        "/api/v1/import/extract",
        files={"file": ("robinhood.csv", text, "text/csv")},
    )


def test_the_route_parses_without_touching_a_provider(client):
    response = post_export(client, REALISTIC)
    assert response.status_code == 200
    body = response.json()
    assert body["broker_format"] == "Robinhood"
    assert body["transaction_count"] == 6
    assert body["rows"] == []
    assert "No AI was used" in body["notes"]
    assert "Nothing was sent to an AI provider" in body["notice"]


def test_the_route_names_the_codes_it_skipped(client):
    """The user has to be able to see what did not come through, or a partial
    import reads as a complete one."""
    body = post_export(client, REALISTIC).json()
    assert [row["code"] for row in body["unknown_codes"]] == ["QQQQ"]


def test_parsed_rows_import_and_re_import_is_a_no_op(client):
    """The point of the stable ids: exports overlap, because you pick a date
    range by hand and nobody picks the same one twice."""
    txns = post_export(client, REALISTIC).json()["transactions"]

    first = client.post("/api/v1/transactions/bulk", json={"transactions": txns}).json()
    assert first["inserted"] == 6
    assert first["skipped"] == 0

    again = client.post("/api/v1/transactions/bulk", json={"transactions": txns}).json()
    assert again["inserted"] == 0
    assert again["skipped"] == 6


def test_an_overlapping_later_export_only_adds_what_is_new(client):
    txns = post_export(client, REALISTIC).json()["transactions"]
    client.post("/api/v1/transactions/bulk", json={"transactions": txns})

    wider = csv_of(
        '"8/22/2026","8/22/2026","","","ACH Deposit","ACH","","","$500.00"',
        *REALISTIC.splitlines()[1:],
    )
    more = post_export(client, wider).json()["transactions"]
    result = client.post("/api/v1/transactions/bulk", json={"transactions": more}).json()
    assert result["inserted"] == 1, "an overlapping export doubled the ledger"


def test_an_option_row_keeps_its_asset_type_into_the_ledger(client):
    """A contract stored as a stock is priced 100x wrong wherever it is used."""
    from backend import db

    text = csv_of(
        '"8/12/2026","8/12/2026","8/13/2026","AAPL","AAPL 8/22/2026 Call $200","BTO","2","$3.50","($700.00)"'
    )
    txns = post_export(client, text).json()["transactions"]
    client.post("/api/v1/transactions/bulk", json={"transactions": txns})
    stored = db.list_transactions(limit=10)
    assert [t.asset_type for t in stored] == ["option"]


def test_the_review_table_can_represent_every_action_the_parser_emits():
    """A cross-language guard, because the failure is silent and expensive.

    The review table renders each action as a <select>. A value with no
    matching <option> does not render as itself — the browser falls back to
    the first option — so a parsed stock split showed up in the UI as a
    deposit, and one touch of that dropdown would have written a contribution
    that never happened straight into the ledger.
    """
    import pathlib
    import re

    source = pathlib.Path("frontend/src/components/SmartImport.jsx").read_text()
    block = re.search(r"const TXN_ACTIONS = \[(.*?)\];", source, re.S)
    assert block, "TXN_ACTIONS moved or was renamed"
    offered = set(re.findall(r"'([a-z_]+)'", block.group(1)))

    emitted = set(broker_csv._RH_SIMPLE.values())
    for inbound, outbound in broker_csv._RH_SIGNED.values():
        emitted.update({inbound, outbound})

    missing = emitted - offered
    assert not missing, f"the parser emits actions the review table cannot show: {sorted(missing)}"


def test_the_summary_line_reads_as_english_in_both_numbers():
    one = broker_csv.parse(csv_of(
        '"8/12/2026","8/12/2026","","XYZ","New","ZZZZ","1","$1.00","$1.00"',
    ))
    assert "1 row used a transaction code" in broker_csv.summarise(one)
    assert "was left out" in broker_csv.summarise(one)

    many = broker_csv.parse(csv_of(
        '"8/12/2026","8/12/2026","","XYZ","New","ZZZZ","1","$1.00","$1.00"',
        '"8/13/2026","8/13/2026","","XYZ","New","YYYY","1","$1.00","$1.00"',
    ))
    assert "2 rows used transaction codes" in broker_csv.summarise(many)
    assert "were left out" in broker_csv.summarise(many)

"""Share splits in the reconstructed history.

Two facts have to hold together, and holding only one of them is worse than
holding neither:

1. The price series is **already split-adjusted at source**. Yahoo's chart
   closes report NVDA at ~$121 the day before its 10:1, not ~$1,210. So
   today's share count is the correct multiplier for every historical close,
   and rescaling the count as well would divide by the split twice.
2. A *transaction's* share count is in the units of its own day. Twelve shares
   bought before a 10:1 are a hundred and twenty of today's shares, and
   subtracting twelve from today's count is what leaves a phantom holding
   standing in every day before the purchase.

The engine therefore restates trades and leaves the running count alone. These
tests pin both halves, because either one applied without the other is a
plausible-looking, badly wrong chart.
"""

from __future__ import annotations

from backend.models import Position, Transaction
from backend import portfolio_history
from backend.portfolio_history import daily_values

DAYS = [
    "2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04",
    "2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08",
]

# A flat, split-adjusted series: the provider has already divided every close
# before the split, so there is no discontinuity here to trip over.
HISTORY = {"NVDA": {"dates": DAYS, "closes": [100.0] * len(DAYS)}}


def position(symbol="NVDA", quantity=120.0, **kw):
    return Position(
        id=1, symbol=symbol, name=symbol, quantity=quantity,
        cost_basis=kw.get("cost_basis", 0.0), price=kw.get("price", 100.0),
        broker="robinhood", asset_type="stock", currency="USD",
    )


def txn(id_, action, quantity, occurred_at, symbol="NVDA", price=0.0, amount=0.0):
    return Transaction(
        id=id_, symbol=symbol, broker="robinhood", asset_type="stock",
        action=action, quantity=quantity, price=price, fee=0.0,
        occurred_at=occurred_at, amount=amount,
    )


def value_on(series, day):
    return next(point["securities"] for point in series if point["date"] == day)


def test_a_pre_split_buy_is_restated_into_todays_shares():
    """The bug this fixes. Twelve shares bought before a 10:1, held through
    it, are a hundred and twenty today — so undoing that purchase has to
    remove all hundred and twenty, leaving nothing before it was made."""
    series = daily_values(
        positions=[position(quantity=120.0)],
        transactions=[txn(1, "buy", 12.0, "2026-01-04", price=1000.0, amount=-12000.0)],
        history=HISTORY,
        splits={"NVDA": [("2026-01-06", 10.0)]},
    )
    assert value_on(series, "2026-01-05") == 12000.0  # 120 adjusted shares x $100
    assert value_on(series, "2026-01-03") == 0.0, "a phantom holding survived the buy"


def test_without_split_information_the_old_error_is_visible():
    """Documents the degraded path rather than hiding it: with no splits
    supplied the engine cannot restate anything, and the phantom remains.
    Pinned so that 'splits are optional' never quietly means 'splits do
    nothing'."""
    series = daily_values(
        positions=[position(quantity=120.0)],
        transactions=[txn(1, "buy", 12.0, "2026-01-04", price=1000.0, amount=-12000.0)],
        history=HISTORY,
    )
    assert value_on(series, "2026-01-03") == 10800.0  # 108 phantom shares


def test_the_running_share_count_is_never_rescaled_by_a_split():
    """Half two of the contract. The series is split-adjusted at source, so a
    holding with no trades at all must be worth the same on both sides of a
    split. Rescaling the count here would divide by the split twice."""
    series = daily_values(
        positions=[position(quantity=120.0)],
        transactions=[],
        history=HISTORY,
        splits={"NVDA": [("2026-01-05", 10.0)]},
    )
    assert {point["securities"] for point in series} == {12000.0}


def test_a_trade_on_the_split_date_is_already_in_the_new_units():
    """A split takes effect at the open, so the same day's fills are post-split
    and must not be restated. Off-by-one here is a 10x error."""
    series = daily_values(
        positions=[position(quantity=120.0)],
        transactions=[txn(1, "buy", 120.0, "2026-01-05", price=100.0, amount=-12000.0)],
        history=HISTORY,
        splits={"NVDA": [("2026-01-05", 10.0)]},
    )
    assert value_on(series, "2026-01-04") == 0.0


def test_a_post_split_trade_is_untouched():
    series = daily_values(
        positions=[position(quantity=120.0)],
        transactions=[txn(1, "buy", 20.0, "2026-01-07", price=100.0, amount=-2000.0)],
        history=HISTORY,
        splits={"NVDA": [("2026-01-03", 10.0)]},
    )
    assert value_on(series, "2026-01-06") == 10000.0  # 100 shares before the buy


def test_two_splits_compound():
    """NVDA really has had two — 4:1 in 2021 and 10:1 in 2024 — so a trade
    before both is off by 40x, not 10."""
    series = daily_values(
        positions=[position(quantity=400.0)],
        transactions=[txn(1, "buy", 10.0, "2026-01-02", price=4000.0, amount=-40000.0)],
        history={"NVDA": {"dates": DAYS, "closes": [100.0] * len(DAYS)}},
        splits={"NVDA": [("2026-01-04", 4.0), ("2026-01-06", 10.0)]},
    )
    assert value_on(series, "2026-01-03") == 40000.0  # 400 adjusted shares
    assert value_on(series, "2026-01-01") == 0.0, "compounding two splits failed"


def test_a_sale_across_a_split_is_restated_too():
    """Sales use the same units and the same correction; only the sign moves."""
    series = daily_values(
        positions=[position(quantity=0.0)],
        transactions=[txn(1, "sell", 5.0, "2026-01-04", price=1000.0, amount=5000.0)],
        history=HISTORY,
        splits={"NVDA": [("2026-01-06", 10.0)]},
    )
    # Sold 5 pre-split shares = 50 of today's, so 50 were held before the sale.
    assert value_on(series, "2026-01-03") == 5000.0


def test_splits_for_a_symbol_never_traded_change_nothing():
    series = daily_values(
        positions=[position(quantity=120.0)],
        transactions=[],
        history=HISTORY,
        splits={"AAPL": [("2026-01-05", 4.0)]},
    )
    assert {point["securities"] for point in series} == {12000.0}


def test_a_nonsense_ratio_is_ignored_rather_than_dividing_by_zero():
    series = daily_values(
        positions=[position(quantity=120.0)],
        transactions=[txn(1, "buy", 120.0, "2026-01-07", price=100.0, amount=-12000.0)],
        history=HISTORY,
        splits={"NVDA": [("2026-01-05", 0.0), ("2026-01-04", -3.0)]},
    )
    assert value_on(series, "2026-01-06") == 0.0


# --- the wiring, not just the maths ----------------------------------------


def test_performance_does_not_pay_for_a_split_lookup_with_no_trades(monkeypatch, tmp_path):
    """A portfolio typed in as holdings has nothing for a split factor to act
    on, and this runs on every page load."""
    from backend import db, portfolio_history

    db.set_db_path(tmp_path / "splits.db")
    db.init_db()
    called = []
    monkeypatch.setattr("backend.prices.fetch_splits", lambda *a, **k: called.append(a) or {})
    portfolio_history.portfolio_performance(
        positions=[position()], transactions=[], history=HISTORY
    )
    assert called == []


def test_performance_looks_up_splits_only_for_traded_symbols(monkeypatch, tmp_path):
    from backend import db, portfolio_history

    db.set_db_path(tmp_path / "splits2.db")
    db.init_db()
    asked = []

    def fake(symbols):
        asked.append(symbols)
        return {"NVDA": [("2026-01-06", 10.0)]}

    monkeypatch.setattr("backend.prices.fetch_splits", fake)
    result = portfolio_history.portfolio_performance(
        positions=[position(quantity=120.0)],
        transactions=[
            txn(1, "buy", 12.0, "2026-01-04", price=1000.0, amount=-12000.0),
            txn(2, "dividend", 0.0, "2026-01-04", symbol="AAPL", price=5.0, amount=5.0),
        ],
        history=HISTORY,
    )
    assert asked == [["NVDA"]], "a dividend-only symbol triggered a split lookup"
    assert result["available"] is True


def test_a_provider_that_cannot_answer_degrades_instead_of_failing(monkeypatch, tmp_path):
    """History that is wrong for pre-split trades is the status quo ante; a
    500 on the dashboard is not."""
    from backend import db, portfolio_history

    db.set_db_path(tmp_path / "splits3.db")
    db.init_db()

    def boom(symbols):
        raise RuntimeError("rate limited")

    monkeypatch.setattr("backend.prices.fetch_splits", boom)
    try:
        portfolio_history.portfolio_performance(
            positions=[position()],
            transactions=[txn(1, "buy", 12.0, "2026-01-04")],
            history=HISTORY,
        )
    except RuntimeError:
        raise AssertionError("a split-lookup failure took down performance") from None


# --- the provider's half ----------------------------------------------------


def test_yahoo_split_events_parse_into_dated_ratios(monkeypatch):
    """Pinned against the real payload shape. Yahoo keys splits by timestamp
    and states the ratio as numerator/denominator, so a 10-for-1 is 10.0/1.0
    — inverting that turns a restatement into a division."""
    from backend.providers import yahoo

    payload = {
        "events": {
            "splits": {
                "1626787800": {"date": 1626787800, "numerator": 4.0,
                               "denominator": 1.0, "splitRatio": "4:1"},
                "1718026200": {"date": 1718026200, "numerator": 10.0,
                               "denominator": 1.0, "splitRatio": "10:1"},
            }
        }
    }
    monkeypatch.setattr(yahoo, "_chart", lambda *a, **k: (payload, None))
    out = yahoo.provider().fetch_splits(["NVDA"], {"NVDA": position()})
    assert out["splits"]["NVDA"] == [("2021-07-20", 4.0), ("2024-06-10", 10.0)]


def test_yahoo_reports_a_symbol_with_no_splits_as_simply_absent(monkeypatch):
    from backend.providers import yahoo

    monkeypatch.setattr(yahoo, "_chart", lambda *a, **k: ({"events": {}}, None))
    assert yahoo.provider().fetch_splits(["AAPL"], {"AAPL": position("AAPL")})["splits"] == {}


def test_a_rate_limited_provider_yields_nothing_and_says_why(monkeypatch):
    """Yahoo 429s readily. The dashboard must survive it."""
    from backend.providers import yahoo

    monkeypatch.setattr(yahoo, "_chart", lambda *a, **k: (None, "HTTP 429"))
    out = yahoo.provider().fetch_splits(["NVDA"], {"NVDA": position()})
    assert out["splits"] == {}
    assert out["errors"] and "429" in out["errors"][0]


def test_options_and_crypto_are_not_asked_about(monkeypatch):
    """Neither splits in the sense this corrects for, and both would waste a
    rate-limited request."""
    from backend.providers import yahoo

    calls = []
    monkeypatch.setattr(yahoo, "_chart", lambda *a, **k: calls.append(a) or ({"events": {}}, None))
    crypto = position("BTC")
    crypto.asset_type = "crypto"
    yahoo.provider().fetch_splits(["BTC"], {"BTC": crypto})
    assert calls == []


# --- when the ledger contradicts the holdings ------------------------------
#
# Rewinding assumes the two agree. A duplicated buy, or a transfer in that was
# never recorded as a purchase, makes them disagree — and undoing more shares
# than were ever held drove the count negative. A negative count priced at a
# positive close produced a negative portfolio value: one real book showed
# securities of -$496,746.85 on the first day of the year, and 148 such days,
# which the chart then rendered as "+$1,059,137.85 (+0.00%)".

FLAT = {"NVDA": {"dates": DAYS, "closes": [100.0] * len(DAYS)}}


def test_a_contradicted_rewind_never_reports_a_negative_holding():
    """Nobody has ever held a negative quantity of a share."""
    series = daily_values(
        # 60 bought against a holding of 10, and no sale to explain it.
        positions=[position(quantity=10.0)],
        transactions=[txn(1, "buy", 60.0, "2026-01-05")],
        history=FLAT,
    )
    assert all(point["securities"] >= 0 for point in series), \
        [p for p in series if p["securities"] < 0]
    assert value_on(series, "2026-01-04") == 0.0
    assert value_on(series, "2026-01-06") == 1000.0


def test_the_contradiction_is_reported_with_the_symbol_and_the_day():
    conflicts: dict[str, str] = {}
    daily_values(positions=[position(quantity=10.0)],
                 transactions=[txn(1, "buy", 60.0, "2026-01-05")],
                 history=FLAT, conflicts=conflicts)
    assert set(conflicts) == {"NVDA"}
    day, impact = conflicts["NVDA"]
    assert day == "2026-01-05"
    assert impact == 5000.0, "50 shares over at $100 is what the reader risks"


def test_reliable_from_is_the_first_day_after_the_contradiction():
    """Everything at or before it is built from a self-contradicting ledger,
    so it is windowed away rather than plotted as measured history."""
    conflicts: dict[str, str] = {}
    series = daily_values(positions=[position(quantity=10.0)],
                          transactions=[txn(1, "buy", 60.0, "2026-01-05")],
                          history=FLAT, conflicts=conflicts)
    assert portfolio_history.reliable_from(series, conflicts, 1000.0) == "2026-01-06"


def test_an_agreeing_ledger_reports_no_conflict_and_no_window():
    conflicts: dict[str, str] = {}
    series = daily_values(positions=[position(quantity=10.0)],
                          transactions=[txn(1, "buy", 4.0, "2026-01-05")],
                          history=FLAT, conflicts=conflicts)
    assert conflicts == {}
    assert portfolio_history.reliable_from(series, conflicts, 1000.0) is None
    assert value_on(series, "2026-01-04") == 600.0


def test_a_sale_with_no_purchase_does_not_count_as_a_contradiction():
    """Rewinding a sale *adds* shares back. That is the ordinary
    incomplete-history case and it stays representable, unlike a negative."""
    conflicts: dict[str, str] = {}
    daily_values(positions=[position(quantity=0.0)],
                 transactions=[txn(1, "sell", 519.0, "2026-01-05")],
                 history=FLAT, conflicts=conflicts)
    assert conflicts == {}


def test_same_day_fills_are_netted_before_the_rewind():
    """A day carries one value in this series, so the order of fills inside it
    is not something the series can represent. Undoing them one at a time
    rewound through states that never existed between the open and the close:
    selling 251 SQQQ and buying 250 back in one session dipped to -250 and was
    recorded as the ledger contradicting the holdings, when the day nets to +1
    and nothing is wrong."""
    conflicts: dict[str, tuple[str, float]] = {}
    series = daily_values(
        positions=[position(quantity=0.0)],
        transactions=[
            txn(1, "sell", 251.0, "2026-01-05"),
            txn(2, "buy", 250.0, "2026-01-05"),
            txn(3, "buy", 1.0, "2026-01-05"),
        ],
        history=FLAT, conflicts=conflicts,
    )
    assert conflicts == {}, "a same-day round trip is not a contradiction"
    assert all(p["securities"] >= 0 for p in series)


def test_an_immaterial_contradiction_does_not_cost_the_whole_year():
    """One share of SPCX out of place — $161 against a $585,370 book — was
    truncating eight months of chart. Trading a rounding error for the whole
    picture is the worse mistake, so the floor is still applied but the
    history stands."""
    conflicts: dict[str, tuple[str, float]] = {}
    series = daily_values(positions=[position(quantity=10.0)],
                          transactions=[txn(1, "buy", 10.01, "2026-01-05")],
                          history=FLAT, conflicts=conflicts)
    assert conflicts, "the contradiction is still recorded"
    assert portfolio_history.reliable_from(series, conflicts, 585_370.0) is None
    assert all(p["securities"] >= 0 for p in series)


def test_a_material_contradiction_still_truncates():
    conflicts: dict[str, tuple[str, float]] = {}
    series = daily_values(positions=[position(quantity=10.0)],
                          transactions=[txn(1, "buy", 60.0, "2026-01-05")],
                          history=FLAT, conflicts=conflicts)
    assert portfolio_history.reliable_from(series, conflicts, 1000.0) == "2026-01-06"


def test_a_sold_out_holding_with_no_closes_does_not_become_a_step():
    """Rewinding a sale puts the shares back. If nothing prices them they are
    worth zero while the proceeds are rewound in full, and the day the sale
    happened becomes a cliff in the line — one 519-share AFRM sale read as a
    $49,538 jump with no cash flow behind it. Priced, the two sides cancel and
    selling changes nothing, which is what selling does."""
    conflicts: dict[str, tuple[str, float]] = {}
    series = daily_values(
        positions=[position(quantity=0.0)],           # sold out
        transactions=[txn(1, "sell", 519.0, "2026-01-05", price=100.0,
                          amount=51_900.0)],
        history=FLAT, conflicts=conflicts,
    )
    before = value_on(series, "2026-01-04")
    after = value_on(series, "2026-01-06")
    assert before == 51_900.0, "the shares were not put back"
    assert after == 0.0
    totals = {p["date"]: p["total"] for p in series}
    assert totals["2026-01-04"] == totals["2026-01-06"], (
        "selling moved the portfolio's value; it only moves where the value sits"
    )


def test_buying_an_option_does_not_read_as_losing_the_money():
    """Options are excluded from the reconstructed share count because no
    historical price series exists for a contract. Counting their cash anyway
    made a $21,700 purchase read as a $21,700 fall in portfolio value, and
    four contracts bought on one day as -$64,208."""
    option = Transaction(
        id=1, symbol="MSFT", broker="robinhood", asset_type="option",
        action="buy", quantity=10, price=21.70, fee=0.0,
        occurred_at="2026-01-05", amount=-21_700.0,
    )
    series = daily_values(positions=[position(quantity=120.0)],
                          transactions=[option], history=FLAT)
    totals = {p["date"]: p["total"] for p in series}
    assert totals["2026-01-04"] == totals["2026-01-06"], (
        "buying an option moved portfolio value; it moves where the value sits, "
        "and the contract side is not tracked"
    )


def test_a_share_trade_still_moves_cash():
    """The guard must not turn into 'ignore every cash effect'."""
    trade = Transaction(
        id=1, symbol="NVDA", broker="robinhood", asset_type="stock",
        action="buy", quantity=10, price=100.0, fee=0.0,
        occurred_at="2026-01-05", amount=-1_000.0,
    )
    series = daily_values(positions=[position(quantity=120.0)],
                          transactions=[trade], history=FLAT)
    cash = {p["date"]: p["cash"] for p in series}
    assert cash["2026-01-04"] == cash["2026-01-06"] + 1_000.0


# --- the series may not begin before the portfolio is priceable ------------


def two_symbol_history(early, late):
    """NVDA priced from `late`, BTC from `early` — the real shape of it: a
    crypto series that opens on a weekend while the equities wait for Monday."""
    return {
        "BTC": {"dates": DAYS, "closes": [1.0] * len(DAYS)},
        "NVDA": {"dates": DAYS[late:], "closes": [100.0] * (len(DAYS) - late)},
    }


def test_the_series_starts_once_the_material_holdings_are_priced():
    """BTC's series opened three days before the equities. Those three days
    priced $136.98 of a $411,000 portfolio, and the day the equities arrived
    chain-linked as a 100% gain — the year's return read +149%."""
    series = daily_values(
        positions=[position(symbol="NVDA", quantity=100.0),
                   position(symbol="BTC", quantity=1.0)],
        transactions=[], history=two_symbol_history(0, 3),
    )
    assert series[0]["date"] == DAYS[3], "began before the equities were priced"
    values = [p["securities"] for p in series]
    assert min(values) > 10_000, f"an unpriced day survived: {values}"


def test_a_tiny_late_listing_does_not_discard_the_whole_history():
    """SPCX listed in June 2026 and is worth a few hundred dollars. Waiting for
    it would throw away ten months to gain a rounding error."""
    history = {
        "NVDA": {"dates": DAYS, "closes": [100.0] * len(DAYS)},
        "SPCX": {"dates": DAYS[6:], "closes": [1.0] * (len(DAYS) - 6)},
    }
    series = daily_values(
        positions=[position(symbol="NVDA", quantity=100.0),
                   position(symbol="SPCX", quantity=1.0)],
        transactions=[], history=history,
    )
    assert series[0]["date"] == DAYS[0]
    assert len(series) == len(DAYS)


def test_a_single_symbol_book_is_untouched():
    series = daily_values(positions=[position(quantity=120.0)],
                          transactions=[], history=FLAT)
    assert len(series) == len(DAYS)


def test_trimming_never_leaves_less_than_two_points():
    """A window too short to measure is worse than a slightly ragged one."""
    history = {
        "NVDA": {"dates": DAYS, "closes": [100.0] * len(DAYS)},
        "AAPL": {"dates": DAYS[-1:], "closes": [500.0]},
    }
    series = daily_values(
        positions=[position(symbol="NVDA", quantity=100.0),
                   position(symbol="AAPL", quantity=100.0)],
        transactions=[], history=history,
    )
    assert len(series) >= 2

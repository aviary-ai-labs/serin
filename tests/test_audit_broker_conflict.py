"""Holdings the connected broker does not report.

A real case: 519 shares of AFRM, entered by hand before the broker was
connected, sold months ago, and still counting $40,357 toward net worth. The
transactions ledger recorded the sale. The brokerage sync did not list the
position. Nothing put those two facts together, so the portfolio was overstated
by forty thousand dollars and every number derived from it was wrong.

Sync deliberately never deletes a row it did not write — removing somebody's
hand-entered data because a vendor failed to mention it would be far worse than
this. So the fix is not to delete, it is to *notice*.
"""

from __future__ import annotations

import pytest
from backend import audit, db
from backend.models import PositionIn


@pytest.fixture
def portfolio(tmp_path):
    db.set_db_path(tmp_path / "audit.db")
    db.init_db()
    return db


def findings(code="unconfirmed_by_broker"):
    return [i for i in audit.audit_portfolio()["issues"] if i["code"] == code]


def test_a_manual_holding_the_synced_broker_omits_is_flagged(portfolio, monkeypatch):
    """The AFRM case exactly."""
    synced = db.create_position(PositionIn(symbol="TQQQ", broker="robinhood",
                                           asset_type="stock", quantity=100,
                                           average_cost=50, current_price=70))
    manual = db.create_position(PositionIn(symbol="AFRM", broker="robinhood",
                                           asset_type="stock", quantity=519,
                                           average_cost=40, current_price=77.76))
    _set_source(synced.id, "snaptrade")
    _set_source(manual.id, "manual")

    found = findings()
    assert len(found) == 1
    assert found[0]["symbol"] == "AFRM"
    assert found[0]["severity"] == "critical"
    assert "not in your robinhood account" in found[0]["title"]
    # The number is the point: an operator has to see what it costs them.
    assert found[0]["evidence"]["market_value"] == pytest.approx(519 * 77.76, rel=1e-6)


def test_a_synced_holding_is_never_flagged(portfolio):
    synced = db.create_position(PositionIn(symbol="TQQQ", broker="robinhood",
                                           asset_type="stock", quantity=100,
                                           average_cost=50, current_price=70))
    _set_source(synced.id, "snaptrade")
    assert findings() == []


def test_a_manual_holding_at_an_unconnected_broker_is_left_alone(portfolio):
    """Most people hold things Serin has no connection to. Flagging those would
    make the audit useless noise — the check depends on the broker being one
    that actually reports."""
    synced = db.create_position(PositionIn(symbol="TQQQ", broker="robinhood",
                                           asset_type="stock", quantity=100,
                                           average_cost=50, current_price=70))
    manual = db.create_position(PositionIn(symbol="VTI", broker="vanguard",
                                           asset_type="stock", quantity=10,
                                           average_cost=200, current_price=250))
    _set_source(synced.id, "snaptrade")
    _set_source(manual.id, "manual")

    found = findings()
    assert [i["symbol"] for i in found] == [], "a broker with no sync was treated as reporting"


def test_a_portfolio_with_no_sync_at_all_raises_nothing(portfolio):
    """Before anyone connects a broker every row is manual, and none of it is
    in conflict with anything."""
    for symbol in ("AAPL", "MSFT", "VTI"):
        created = db.create_position(PositionIn(symbol=symbol, broker="manual",
                                                asset_type="stock", quantity=5,
                                                average_cost=100, current_price=110))
        _set_source(created.id, "manual")
    assert findings() == []


def test_cash_is_not_flagged(portfolio):
    """Cash rows are bookkeeping, not holdings a broker confirms symbol by
    symbol, and they routinely coexist with synced ones."""
    synced = db.create_position(PositionIn(symbol="TQQQ", broker="robinhood",
                                           asset_type="stock", quantity=100,
                                           average_cost=50, current_price=70))
    cash = db.create_position(PositionIn(symbol="CASH", broker="robinhood",
                                         asset_type="cash", quantity=5000,
                                         average_cost=1, current_price=1))
    _set_source(synced.id, "snaptrade")
    _set_source(cash.id, "manual")
    assert findings() == []


def test_the_finding_is_critical_because_it_overstates_net_worth(portfolio):
    """Severity is not decoration — it decides what the audit surfaces first.
    A phantom holding inflates the total, every allocation percentage, and
    every return computed against them."""
    synced = db.create_position(PositionIn(symbol="TQQQ", broker="robinhood",
                                           asset_type="stock", quantity=100,
                                           average_cost=50, current_price=70))
    manual = db.create_position(PositionIn(symbol="AFRM", broker="robinhood",
                                           asset_type="stock", quantity=519,
                                           average_cost=40, current_price=77.76))
    _set_source(synced.id, "snaptrade")
    _set_source(manual.id, "manual")

    report = audit.audit_portfolio()
    assert report["issue_counts"]["critical"] >= 1
    assert report["issues"][0]["code"] == "unconfirmed_by_broker", (
        "the most expensive finding is not being surfaced first"
    )


def _set_source(position_id: int, source: str) -> None:
    """Positions carry their source in a column the public API does not set."""
    with db.connect() as conn:
        conn.execute("UPDATE positions SET source=? WHERE id=?", (source, position_id))


# --- a quantity the broker disagreed with ----------------------------------
# positions are UNIQUE(user_id, symbol, broker, asset_type), so a sync does not
# create a second row to compare against — it overwrites. After the write the
# disagreement is unrecoverable, which is why it has to be captured on the way
# past rather than detected afterwards.


def _synced(symbol, broker, quantity, source="snaptrade"):
    return PositionIn(symbol=symbol, broker=broker, asset_type="stock",
                      quantity=quantity, average_cost=100, current_price=110)


def test_a_sync_that_overwrites_a_hand_entered_quantity_records_it(portfolio):
    typed = db.create_position(_synced("AFRM", "robinhood", 500))
    _set_source(typed.id, "manual")

    result = db.replace_synced_positions([_synced("AFRM", "robinhood", 400)], {"robinhood"})
    assert len(result["conflicts"]) == 1
    conflict = result["conflicts"][0]
    assert conflict["entered_quantity"] == 500 and conflict["synced_quantity"] == 400
    assert conflict["entered_source"] == "manual"

    found = findings("quantity_overwritten_by_sync")
    assert len(found) == 1
    assert "disagreed with your broker" in found[0]["title"]
    assert found[0]["evidence"]["difference"] == -100


def test_the_brokers_number_still_wins_the_write(portfolio):
    """Recording the disagreement must not change who is authoritative."""
    typed = db.create_position(_synced("AFRM", "robinhood", 500))
    _set_source(typed.id, "manual")
    db.replace_synced_positions([_synced("AFRM", "robinhood", 400)], {"robinhood"})
    held = {p.symbol: p.quantity for p in db.list_positions()}
    assert held["AFRM"] == 400


def test_agreeing_quantities_record_nothing(portfolio):
    typed = db.create_position(_synced("AFRM", "robinhood", 400))
    _set_source(typed.id, "manual")
    result = db.replace_synced_positions([_synced("AFRM", "robinhood", 400)], {"robinhood"})
    assert result["conflicts"] == []
    assert findings("quantity_overwritten_by_sync") == []


def test_fractional_reinvestment_precision_is_not_a_conflict(portfolio):
    """Brokers report dividend reinvestment to six decimals; people type two.
    Flagging 387.128022 against 387.13 would cry wolf on every holding."""
    typed = db.create_position(_synced("NKE", "robinhood", 387.13))
    _set_source(typed.id, "manual")
    result = db.replace_synced_positions([_synced("NKE", "robinhood", 387.128022)], {"robinhood"})
    assert result["conflicts"] == []


def test_a_resynced_position_of_its_own_source_is_not_a_conflict(portfolio):
    """Quantities change because people trade. Only a disagreement *between
    sources* is worth anyone's attention."""
    db.replace_synced_positions([_synced("TQQQ", "robinhood", 100)], {"robinhood"})
    result = db.replace_synced_positions([_synced("TQQQ", "robinhood", 250)], {"robinhood"})
    assert result["conflicts"] == []


def test_fixing_the_position_clears_the_warning_on_the_next_sync(portfolio):
    """No acknowledgement flow, so the record has to expire by itself or it
    becomes a permanent scold for something already fixed."""
    typed = db.create_position(_synced("AFRM", "robinhood", 500))
    _set_source(typed.id, "manual")
    db.replace_synced_positions([_synced("AFRM", "robinhood", 400)], {"robinhood"})
    assert findings("quantity_overwritten_by_sync")

    db.replace_synced_positions([_synced("AFRM", "robinhood", 400)], {"robinhood"})
    assert findings("quantity_overwritten_by_sync") == []


# --- the same purchase recorded twice --------------------------------------


def _lot(symbol, broker, acquired, quantity, cost):
    from backend.models import TaxLotIn

    return db.create_tax_lot(TaxLotIn(symbol=symbol, broker=broker, quantity=quantity,
                                      cost_basis=cost, acquired_at=acquired))


def test_the_same_lot_entered_twice_is_flagged(portfolio):
    _lot("AAPL", "manual", "2026-03-02", 100, 150.0)
    _lot("AAPL", "manual", "2026-03-02", 100, 150.0)
    found = findings("duplicate_tax_lot")
    assert len(found) == 1
    assert found[0]["evidence"]["lot_count"] == 2
    assert found[0]["evidence"]["acquired_at"] == "2026-03-02"


def test_two_different_fills_on_one_day_are_not_duplicates(portfolio):
    """Buying the same stock twice in a day is ordinary, which is exactly why
    tax lots carry no uniqueness constraint."""
    _lot("AAPL", "manual", "2026-03-02", 100, 150.0)
    _lot("AAPL", "manual", "2026-03-02", 40, 151.0)
    assert findings("duplicate_tax_lot") == []


def test_the_same_quantity_at_a_different_price_is_not_a_duplicate(portfolio):
    _lot("AAPL", "manual", "2026-03-02", 100, 150.0)
    _lot("AAPL", "manual", "2026-03-02", 100, 152.5)
    assert findings("duplicate_tax_lot") == []


def test_lots_on_different_days_are_not_duplicates(portfolio):
    _lot("AAPL", "manual", "2026-03-02", 100, 150.0)
    _lot("AAPL", "manual", "2026-03-03", 100, 150.0)
    assert findings("duplicate_tax_lot") == []

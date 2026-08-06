"""Scope isolation — the contract that keeps one user's data out of another's.

These live in core even though identity lives in the commercial pack: the pack
decides *who* the user is, but core owns the guarantee that a scope can only
ever see its own rows, and must not be able to break it while its own suite
stays green.

The failure this guards against is asymmetric. Single-user, a missing filter
shows you your own data. Shared, it shows you everyone's — so every scoped
read AND write is exercised here, and market-data caches are asserted to be
deliberately *shared* (scoping them would multiply the provider bill by the
user count for identical data).
"""

from __future__ import annotations

import pytest
from backend import db, dbdriver, scope
from backend.models import AccountIn, PositionIn, TaxLotIn, TransactionIn

A = "user-a"
B = "user-b"


def _fresh(tmp_path):
    """A clean database on whichever driver is configured.

    With SERIN_DATABASE_URL pointing at Postgres the whole contract below runs
    against the real thing — RLS included — so isolation is proven on the
    engine that will actually serve users, not only on SQLite.
    """
    if dbdriver.is_postgres():
        db.init_db()
        # Deletes are RLS-scoped, so clearing means visiting each scope in turn
        # — itself a small demonstration that the policy is live.
        for owner in (A, B, scope.LOCAL_SCOPE):
            with scope.using(owner), db.connect() as conn:
                for table in ("positions", "tax_lots", "transactions",
                              "briefings", "accounts", "app_settings"):
                    conn.execute(f"DELETE FROM {table}")
        with db.connect() as conn:
            conn.execute("DELETE FROM price_history")
            conn.execute("DELETE FROM fundamentals")
        return
    db.set_db_path(tmp_path / "scope.db")
    db.init_db()


# --- the seam itself -------------------------------------------------------


def test_defaults_to_local_with_no_provider():
    """Self-host: no provider installed, so every call is one implicit user.
    This is what keeps the free tier free of accounts."""
    scope.set_scope_provider(None)
    assert scope.current() == scope.LOCAL_SCOPE


def test_using_overrides_and_restores():
    scope.set_scope_provider(None)
    with scope.using(A):
        assert scope.current() == A
        with scope.using(B):
            assert scope.current() == B
        assert scope.current() == A
    assert scope.current() == scope.LOCAL_SCOPE


def test_provider_failure_raises_rather_than_falling_back():
    """A provider is only installed in multi-user mode. If it fails there,
    guessing 'local' would pool every user into one shared dataset — so this
    fails closed, unlike the entitlements verifier which fails open to OSS."""
    def broken():
        raise RuntimeError("session backend down")

    scope.set_scope_provider(broken)
    try:
        with pytest.raises(scope.ScopeError):
            scope.current()
    finally:
        scope.set_scope_provider(None)


def test_provider_returning_empty_raises():
    scope.set_scope_provider(lambda: "")
    try:
        with pytest.raises(scope.ScopeError):
            scope.current()
    finally:
        scope.set_scope_provider(None)


# --- per-user tables: reads ------------------------------------------------


def test_positions_are_isolated(tmp_path):
    _fresh(tmp_path)
    with scope.using(A):
        db.create_position(PositionIn(symbol="AAPL", broker="schwab", quantity=10,
                                      average_cost=180, current_price=225))
    with scope.using(B):
        db.create_position(PositionIn(symbol="TSLA", broker="schwab", quantity=5,
                                      average_cost=200, current_price=250))
        assert [p.symbol for p in db.list_positions()] == ["TSLA"]
    with scope.using(A):
        assert [p.symbol for p in db.list_positions()] == ["AAPL"]


def test_same_symbol_and_broker_allowed_for_both_users(tmp_path):
    """UNIQUE(symbol, broker, asset_type) was global — two users holding AAPL
    at the same broker would have collided."""
    _fresh(tmp_path)
    with scope.using(A):
        db.create_position(PositionIn(symbol="AAPL", broker="schwab", quantity=10,
                                      average_cost=180, current_price=225))
    with scope.using(B):
        db.create_position(PositionIn(symbol="AAPL", broker="schwab", quantity=99,
                                      average_cost=100, current_price=225))
        assert db.list_positions()[0].quantity == 99
    with scope.using(A):
        assert db.list_positions()[0].quantity == 10


def test_tax_lots_transactions_briefings_accounts_isolated(tmp_path):
    _fresh(tmp_path)
    with scope.using(A):
        db.create_tax_lot(TaxLotIn(symbol="AAPL", broker="schwab", quantity=5,
                                   cost_basis=900, acquired_at="2026-01-02"))
        db.create_transaction(TransactionIn(symbol="AAPL", action="buy", quantity=5,
                                            price=180, occurred_at="2026-01-02"))
        db.create_briefing({"positions": []}, model="test-model")
        db.create_account(AccountIn(name="Roth IRA", broker="schwab"))
    with scope.using(B):
        assert db.list_tax_lots() == []
        assert db.list_transactions() == []
        assert db.list_briefings() == []
        assert db.list_accounts(with_summary=False) == []


def test_account_name_may_repeat_across_users(tmp_path):
    """accounts.name was globally UNIQUE — only one person could own a
    'Roth IRA' on the whole deployment."""
    _fresh(tmp_path)
    with scope.using(A):
        db.create_account(AccountIn(name="Roth IRA", broker="schwab"))
    with scope.using(B):
        db.create_account(AccountIn(name="Roth IRA", broker="fidelity"))
        assert [a.broker for a in db.list_accounts(with_summary=False)] == ["fidelity"]


def test_settings_are_per_user(tmp_path):
    """app_settings.key was the primary key, so one person changing their
    display currency would have changed everyone's."""
    _fresh(tmp_path)
    with scope.using(A):
        db.set_setting("display_currency", "EUR")
    with scope.using(B):
        assert db.get_setting("display_currency", "USD") == "USD"
        db.set_setting("display_currency", "JPY")
    with scope.using(A):
        assert db.get_setting("display_currency") == "EUR"


# --- per-user tables: writes can't reach across ----------------------------


def test_cannot_read_or_mutate_another_scopes_position(tmp_path):
    _fresh(tmp_path)
    with scope.using(A):
        pos = db.create_position(PositionIn(symbol="AAPL", broker="schwab", quantity=10,
                                            average_cost=180, current_price=225))
    with scope.using(B):
        assert db.get_position(pos.id) is None
        assert db.update_position(pos.id, PositionIn(symbol="AAPL", broker="schwab",
                                                     quantity=999, average_cost=1,
                                                     current_price=1)) is None
        assert db.delete_position(pos.id) is False
    with scope.using(A):
        assert db.get_position(pos.id).quantity == 10  # untouched


def test_cannot_delete_another_scopes_rows(tmp_path):
    _fresh(tmp_path)
    with scope.using(A):
        lot = db.create_tax_lot(TaxLotIn(symbol="AAPL", broker="schwab", quantity=5,
                                         cost_basis=900, acquired_at="2026-01-02"))
        txn = db.create_transaction(TransactionIn(symbol="AAPL", action="buy", quantity=5,
                                                  price=180, occurred_at="2026-01-02"))
        brief = db.create_briefing({"positions": []}, model="test-model")
        acct = db.create_account(AccountIn(name="Roth IRA", broker="schwab"))
    with scope.using(B):
        assert db.delete_tax_lot(lot.id) is False
        assert db.delete_transaction(txn.id) is False
        assert db.delete_briefing(brief.id) is False
        assert db.delete_account(acct.id) is False
    with scope.using(A):
        assert len(db.list_tax_lots()) == 1
        assert len(db.list_transactions()) == 1
        assert len(db.list_briefings()) == 1
        assert len(db.list_accounts(with_summary=False)) == 1


def test_summaries_only_count_own_rows(tmp_path):
    _fresh(tmp_path)
    with scope.using(A):
        db.create_position(PositionIn(symbol="AAPL", broker="schwab", quantity=10,
                                      average_cost=100, current_price=200))
    with scope.using(B):
        db.create_position(PositionIn(symbol="TSLA", broker="schwab", quantity=10,
                                      average_cost=100, current_price=300))
        assert db.portfolio_summary().total_value == pytest.approx(3000)
    with scope.using(A):
        assert db.portfolio_summary().total_value == pytest.approx(2000)


def test_bulk_broker_delete_stops_at_the_scope_boundary(tmp_path):
    """replace_synced_positions / delete_positions_for_brokers wipe by broker —
    the widest blast radius in the module."""
    _fresh(tmp_path)
    with scope.using(A):
        db.create_position(PositionIn(symbol="AAPL", broker="schwab", quantity=10,
                                      average_cost=180, current_price=225))
    with scope.using(B):
        db.create_position(PositionIn(symbol="AAPL", broker="schwab", quantity=7,
                                      average_cost=180, current_price=225))
        db.delete_positions_for_brokers({"schwab"}, source="manual")
        assert db.list_positions() == []
    with scope.using(A):
        assert len(db.list_positions()) == 1  # survived B's wipe


# --- shared caches: deliberately NOT scoped --------------------------------


def test_price_history_cache_is_shared(tmp_path):
    """Market data is identical for everyone. Scoping it would refetch the same
    symbol once per user and multiply the provider bill by the user count."""
    _fresh(tmp_path)
    with scope.using(A):
        db.cache_price_history({"AAPL": {"dates": ["2026-01-02", "2026-01-03"],
                                         "closes": [225.0, 228.0]}})
    with scope.using(B):
        cached = db.get_cached_price_history(["AAPL"])
        assert cached["AAPL"]["closes"] == [225.0, 228.0]


def test_fundamentals_cache_is_shared(tmp_path):
    _fresh(tmp_path)
    with scope.using(A):
        db.upsert_fundamentals("AAPL", {"sector": "Technology"})
    with scope.using(B):
        cached = db.get_cached_fundamentals(["AAPL"])
        assert cached["AAPL"]["payload"]["sector"] == "Technology"


# --- upgrade path ----------------------------------------------------------


@pytest.mark.skipif(
    dbdriver.is_postgres(),
    reason="replays the SQLite migration history; Postgres is created fresh at the current schema",
)
def test_existing_database_upgrades_without_losing_data(tmp_path):
    """A self-hoster upgrading must keep every row, and land on LOCAL_SCOPE so
    the app behaves exactly as before. Migration 6 rebuilds three tables, so it
    is the migration most capable of destroying someone's portfolio."""
    import sqlite3 as sq

    path = tmp_path / "legacy.db"
    db.set_db_path(path)

    # Build a genuine pre-scoping database: migrations 1..5, and rows written
    # the way that schema wrote them (no user_id column exists yet).
    original = db.MIGRATIONS
    db.MIGRATIONS = [m for m in original if m[0] <= 5]
    try:
        db.init_db()
        assert db.schema_version() == 5
        with sq.connect(path) as conn:
            conn.execute(
                """INSERT INTO positions (symbol, name, broker, asset_type, quantity,
                       average_cost, current_price, sector, updated_at, source, currency)
                   VALUES ('AAPL','Apple','schwab','stock',10,180,225,'Tech','2026-01-02','manual','USD')"""
            )
            conn.execute(
                "INSERT INTO accounts (name, kind, broker, currency, created_at)"
                " VALUES ('Roth IRA','roth_ira','schwab','USD','2026-01-02')"
            )
            conn.execute("INSERT INTO app_settings (key, value) VALUES ('display_currency','EUR')")
            conn.execute(
                "INSERT INTO tax_lots (symbol, broker, quantity, cost_basis, acquired_at, created_at)"
                " VALUES ('AAPL','schwab',5,900,'2026-01-02','2026-01-02')"
            )
    finally:
        db.MIGRATIONS = original

    db.init_db()  # applies migration 6
    assert db.schema_version() == 6

    # Everything survived, and belongs to the single local owner.
    assert [p.symbol for p in db.list_positions()] == ["AAPL"]
    assert [a.name for a in db.list_accounts(with_summary=False)] == ["Roth IRA"]
    assert db.get_setting("display_currency") == "EUR"
    assert len(db.list_tax_lots()) == 1

    with sq.connect(path) as conn:
        owners = {row[0] for row in conn.execute("SELECT DISTINCT user_id FROM positions")}
    assert owners == {scope.LOCAL_SCOPE}

    # Re-running is a no-op — migrations stay idempotent.
    db.init_db()
    assert len(db.list_positions()) == 1

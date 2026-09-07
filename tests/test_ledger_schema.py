"""Storage for transaction-accurate performance.

Two acceptance cases in the performance brief are storage problems before
they are arithmetic problems: a statement imported twice must not duplicate,
and a fully sold position must keep its realized result. Neither was
expressible in the old schema.
"""

from __future__ import annotations

import sqlite3

import pytest
from backend import db, scope
from backend.models import TransactionIn


@pytest.fixture
def store(tmp_path):
    db.set_db_path(tmp_path / "ledger.db")
    db.init_db()
    return db


def _columns(table: str) -> set[str]:
    with db.connect() as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_a_fresh_database_matches_a_migrated_one(tmp_path):
    """The baseline creates the modern shape and the migrations upgrade an old
    one to the same place. When those drift, the bug only shows on whichever
    kind of install nobody tested."""
    fresh_cols = {}
    db.set_db_path(tmp_path / "fresh.db")
    db.init_db()
    for table in ("transactions", "tax_lots"):
        fresh_cols[table] = _columns(table)

    # An old database: baseline only, then every migration applied.
    old = tmp_path / "old.db"
    conn = sqlite3.connect(old)
    conn.executescript(
        """CREATE TABLE transactions (
             id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL DEFAULT '',
             broker TEXT NOT NULL DEFAULT 'manual', asset_type TEXT NOT NULL DEFAULT 'stock',
             action TEXT NOT NULL DEFAULT 'buy', quantity REAL NOT NULL DEFAULT 0,
             price REAL NOT NULL DEFAULT 0, fee REAL NOT NULL DEFAULT 0,
             amount REAL NOT NULL DEFAULT 0, currency TEXT NOT NULL DEFAULT 'USD',
             occurred_at TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
             source TEXT NOT NULL DEFAULT 'manual', created_at TEXT NOT NULL,
             user_id TEXT NOT NULL DEFAULT 'local');
           CREATE TABLE tax_lots (
             id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
             broker TEXT NOT NULL DEFAULT 'manual', quantity REAL NOT NULL DEFAULT 0,
             cost_basis REAL NOT NULL DEFAULT 0, acquired_at TEXT NOT NULL,
             created_at TEXT NOT NULL, user_id TEXT NOT NULL DEFAULT 'local');"""
    )
    conn.commit()
    conn.close()

    db.set_db_path(old)
    from backend.db import _migration_ledger_lifecycle

    with db.connect() as c:
        _migration_ledger_lifecycle(c)
    for table in ("transactions", "tax_lots"):
        missing = fresh_cols[table] - _columns(table)
        assert not missing, f"{table}: migration missed {sorted(missing)}"


def test_existing_lots_are_backfilled_as_fully_open(store):
    """A lot that predates the lifecycle columns is open, so all of it remains.
    Defaulting remaining_quantity to 0 would silently close every lot a
    customer already had."""
    with scope.using("local"), db.connect() as conn:
        conn.execute(
            """INSERT INTO tax_lots (symbol, broker, quantity, cost_basis, acquired_at,
                                     remaining_quantity, created_at, user_id)
               VALUES ('NFLX','fidelity',300,30000,'2024-01-05',-1,'2024-01-05','local')"""
        )
    from backend.db import _migration_ledger_lifecycle

    with db.connect() as conn:
        _migration_ledger_lifecycle(conn)
        remaining = conn.execute(
            "SELECT remaining_quantity FROM tax_lots WHERE symbol='NFLX'"
        ).fetchone()[0]
    assert remaining == 300


def test_hand_entered_transactions_may_repeat(store):
    """Buying the same thing twice in one day is ordinary. Only fingerprinted
    imports are held unique, which is why the index is partial."""
    row = TransactionIn(
        symbol="AAPL", action="buy", quantity=1, price=100.0, occurred_at="2026-01-05"
    )
    db.create_transaction(row)
    db.create_transaction(row)
    assert len(db.list_transactions(symbol="AAPL")) == 2


def test_an_imported_row_cannot_land_twice(store):
    """Re-importing January's statement in February must update, not double.
    Doubling contributions corrupts every return that depends on them."""
    with scope.using("local"), db.connect() as conn:
        conn.execute(
            """INSERT INTO transactions (symbol, action, quantity, price, occurred_at,
                                         external_id, created_at, user_id)
               VALUES ('AAPL','buy',1,100,'2026-01-05','stmt:jan:1','2026-01-05','local')"""
        )
        # The unique index is what refuses it; the driver's exact exception
        # class differs between SQLite and Postgres, so match on both.
        with pytest.raises((sqlite3.IntegrityError, sqlite3.DatabaseError)):
            conn.execute(
                """INSERT INTO transactions (symbol, action, quantity, price, occurred_at,
                                             external_id, created_at, user_id)
                   VALUES ('AAPL','buy',1,100,'2026-01-05','stmt:jan:1','2026-01-05','local')"""
            )


def test_the_dedupe_key_is_per_account(store):
    """Two customers importing statements from the same broker will collide on
    any fingerprint the broker generates. The index must be scoped."""
    with scope.using("u_alice"), db.connect() as conn:
        conn.execute(
            """INSERT INTO transactions (symbol, action, quantity, price, occurred_at,
                                         external_id, created_at, user_id)
               VALUES ('AAPL','buy',1,100,'2026-01-05','fidelity:txn:9','2026-01-05','u_alice')"""
        )
    with scope.using("u_bob"), db.connect() as conn:
        conn.execute(
            """INSERT INTO transactions (symbol, action, quantity, price, occurred_at,
                                         external_id, created_at, user_id)
               VALUES ('AAPL','buy',1,100,'2026-01-05','fidelity:txn:9','2026-01-05','u_bob')"""
        )


def test_a_newly_created_lot_is_open(store):
    """Relying on the column default would create every lot already sold: the
    quantity gone, the cost basis stranded, and nothing left to value."""
    from backend.models import TaxLotIn

    lot = db.create_tax_lot(
        TaxLotIn(symbol="NFLX", broker="fidelity", quantity=300, cost_basis=30000.0,
                 acquired_at="2024-01-05")
    )
    with db.connect() as conn:
        remaining = conn.execute(
            "SELECT remaining_quantity FROM tax_lots WHERE id=?", (lot.id,)
        ).fetchone()[0]
    assert remaining == 300


def test_a_connector_source_does_not_break_the_portfolio(store):
    """Position.source was a closed Literal while coinbase and binance — both
    default-enabled — wrote ids outside it. Any user who synced either one got
    a ValidationError out of list_positions(), i.e. a 500 for their whole
    portfolio. Connectors are a plugin system; the field cannot be closed."""
    with scope.using("local"), db.connect() as conn:
        for source in ("coinbase", "binance", "snaptrade", "csv", "manual", "some_future_broker"):
            conn.execute(
                """INSERT INTO positions
                     (user_id, symbol, name, broker, asset_type, quantity, average_cost,
                      current_price, sector, currency, source, updated_at)
                   VALUES ('local', ?, ?, ?, 'crypto', 1, 100, 200, '', 'USD', ?, '2026-01-01T00:00:00Z')""",
                (source.upper()[:8], source, source, source),
            )
    assert len(db.list_positions()) == 6


def test_closed_positions_are_hidden_but_kept(store):
    """The whole point of the change: a sold holding leaves the dashboard and
    stays in the record, because deleting it is what erases last year's return."""
    from backend.models import PositionIn

    kept = db.create_position(
        PositionIn(symbol="NFLX", name="Netflix", broker="fidelity", asset_type="stock",
                   quantity=300, average_cost=100.0, current_price=120.0)
    )
    db.update_position(
        kept.id,
        PositionIn(symbol="NFLX", name="Netflix", broker="fidelity", asset_type="stock",
                   quantity=0, average_cost=100.0, current_price=120.0),
    )
    assert [p.symbol for p in db.list_positions()] == []
    assert [p.symbol for p in db.list_positions(include_closed=True)] == ["NFLX"]

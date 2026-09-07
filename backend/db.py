from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from backend import dbdriver, scope
from backend.config import settings
from backend.models import (
    Account,
    AccountIn,
    Briefing,
    PortfolioSummary,
    Position,
    PositionIn,
    TaxLot,
    TaxLotIn,
    Transaction,
    TransactionIn,
    utcnow_iso,
)

logger = logging.getLogger(__name__)

DB_PATH: Path = settings.db_path


def set_db_path(path: Path) -> None:
    global DB_PATH
    DB_PATH = path
    # The cache describes the settings in whichever database was live; another
    # one has its own. Resolved at call time, so the forward reference is fine.
    forget_settings_cache()


@contextmanager
def connect() -> Iterator[Any]:
    """A connection on whichever driver is configured (see backend.dbdriver).

    The scope is bound to the session on checkout so Postgres row-level
    security can enforce isolation underneath the filters in this module.
    """
    with dbdriver.connect(DB_PATH) as conn:
        dbdriver.bind_scope(conn, scope.current())
        yield conn


# --- versioned migrations ---------------------------------------------------
# Applied in order, recorded in schema_version, idempotent against a database
# that predates the version table (every step uses IF NOT EXISTS / column
# guards). Add new schema changes as new numbered entries — never edit an
# applied migration.


def _migration_baseline(conn: sqlite3.Connection) -> None:
    _create_baseline_schema(conn)


def _migration_briefing_columns(conn: sqlite3.Connection) -> None:
    briefing_cols = {row[1] for row in conn.execute("PRAGMA table_info(briefings)")}
    if "trigger" not in briefing_cols:
        conn.execute("ALTER TABLE briefings ADD COLUMN trigger TEXT NOT NULL DEFAULT 'manual'")
    if "emailed_at" not in briefing_cols:
        conn.execute("ALTER TABLE briefings ADD COLUMN emailed_at TEXT")


def _migration_position_source(conn: sqlite3.Connection) -> None:
    position_cols = {row[1] for row in conn.execute("PRAGMA table_info(positions)")}
    if "source" not in position_cols:
        conn.execute("ALTER TABLE positions ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")


def _migration_position_currency(conn: sqlite3.Connection) -> None:
    position_cols = {row[1] for row in conn.execute("PRAGMA table_info(positions)")}
    if "currency" not in position_cols:
        conn.execute("ALTER TABLE positions ADD COLUMN currency TEXT NOT NULL DEFAULT 'USD'")


def _migration_fundamentals(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS fundamentals (
             symbol TEXT PRIMARY KEY,
             payload TEXT NOT NULL,
             fetched_at TEXT NOT NULL
           )"""
    )


def _migration_shared_quotes(conn: sqlite3.Connection) -> None:
    """The shared quote cache and its work-list.

    Both are also in the baseline for fresh databases; existing self-hosters
    are at version 6 and never replay it, so they arrive here instead.
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS quotes (
             symbol TEXT NOT NULL,
             asset_type TEXT NOT NULL DEFAULT 'stock',
             price REAL NOT NULL,
             sector TEXT NOT NULL DEFAULT '',
             updated_at TEXT NOT NULL,
             PRIMARY KEY (symbol, asset_type)
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tracked_symbols (
             symbol TEXT NOT NULL,
             asset_type TEXT NOT NULL DEFAULT 'stock',
             updated_at TEXT NOT NULL,
             PRIMARY KEY (symbol, asset_type)
           )"""
    )
    # Seed the work-list from what this database already holds. Safe here and
    # nowhere else: a migration runs before any request, on one deployment's
    # own file, so there is no tenant boundary to cross.
    conn.execute(
        """INSERT INTO tracked_symbols (symbol, asset_type, updated_at)
           SELECT DISTINCT UPPER(symbol), asset_type, ?
             FROM positions
            WHERE asset_type NOT IN ('cash', 'option')
           ON CONFLICT(symbol, asset_type) DO NOTHING""",
        (utcnow_iso(),),
    )


def _migration_user_scope(conn: sqlite3.Connection) -> None:
    """Partition the per-user tables by ``user_id`` (see ``backend.scope``).

    Existing rows become :data:`scope.LOCAL_SCOPE`, which is exactly what a
    self-hosted instance keeps using forever — so this is invisible unless a
    pack installs a scope provider.

    Three tables need a full rebuild rather than ``ADD COLUMN``, because their
    uniqueness was global and would collide the moment a second user existed:

    - ``positions.UNIQUE(symbol, broker, asset_type)`` — two users could not
      both hold AAPL at the same broker.
    - ``accounts.name UNIQUE`` — only one person on the deployment could own a
      "Roth IRA".
    - ``app_settings.key PRIMARY KEY`` — one person's display currency was
      everyone's.

    The market-data caches (``price_history``, ``fundamentals``, ``fx_rates``)
    are deliberately left alone: identical for every user, and scoping them
    would refetch each symbol once per user.
    """
    def has_column(table: str, column: str) -> bool:
        return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))

    # Simple appends — no constraint touches uniqueness on these.
    for table in ("tax_lots", "transactions", "briefings"):
        if not has_column(table, "user_id"):
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN user_id TEXT NOT NULL DEFAULT '{scope.LOCAL_SCOPE}'"
            )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table}_user ON {table}(user_id)"
        )

    # --- rebuilds (SQLite cannot alter a UNIQUE/PRIMARY KEY in place) ------
    if not has_column("positions", "user_id"):
        conn.executescript(
            f"""
            CREATE TABLE positions_scoped (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id TEXT NOT NULL DEFAULT '{scope.LOCAL_SCOPE}',
              symbol TEXT NOT NULL,
              name TEXT NOT NULL DEFAULT '',
              broker TEXT NOT NULL DEFAULT 'manual',
              asset_type TEXT NOT NULL DEFAULT 'stock',
              quantity REAL NOT NULL DEFAULT 0,
              average_cost REAL NOT NULL DEFAULT 0,
              current_price REAL NOT NULL DEFAULT 0,
              sector TEXT NOT NULL DEFAULT '',
              updated_at TEXT NOT NULL,
              source TEXT NOT NULL DEFAULT 'manual',
              currency TEXT NOT NULL DEFAULT 'USD',
              UNIQUE(user_id, symbol, broker, asset_type)
            );
            INSERT INTO positions_scoped
              (id, user_id, symbol, name, broker, asset_type, quantity,
               average_cost, current_price, sector, updated_at, source, currency)
              SELECT id, '{scope.LOCAL_SCOPE}', symbol, name, broker, asset_type,
                     quantity, average_cost, current_price, sector, updated_at,
                     source, currency
                FROM positions;
            DROP TABLE positions;
            ALTER TABLE positions_scoped RENAME TO positions;
            CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol);
            CREATE INDEX IF NOT EXISTS idx_positions_broker ON positions(broker);
            CREATE INDEX IF NOT EXISTS idx_positions_user ON positions(user_id);
            """
        )

    if not has_column("accounts", "user_id"):
        conn.executescript(
            f"""
            CREATE TABLE accounts_scoped (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id TEXT NOT NULL DEFAULT '{scope.LOCAL_SCOPE}',
              name TEXT NOT NULL,
              kind TEXT NOT NULL DEFAULT 'taxable',
              broker TEXT NOT NULL DEFAULT 'manual',
              currency TEXT NOT NULL DEFAULT 'USD',
              created_at TEXT NOT NULL,
              UNIQUE(user_id, name)
            );
            INSERT INTO accounts_scoped
              (id, user_id, name, kind, broker, currency, created_at)
              SELECT id, '{scope.LOCAL_SCOPE}', name, kind, broker, currency, created_at
                FROM accounts;
            DROP TABLE accounts;
            ALTER TABLE accounts_scoped RENAME TO accounts;
            CREATE INDEX IF NOT EXISTS idx_accounts_user ON accounts(user_id);
            """
        )

    if not has_column("app_settings", "user_id"):
        conn.executescript(
            f"""
            CREATE TABLE app_settings_scoped (
              user_id TEXT NOT NULL DEFAULT '{scope.LOCAL_SCOPE}',
              key TEXT NOT NULL,
              value TEXT NOT NULL,
              PRIMARY KEY (user_id, key)
            );
            INSERT INTO app_settings_scoped (user_id, key, value)
              SELECT '{scope.LOCAL_SCOPE}', key, value FROM app_settings;
            DROP TABLE app_settings;
            ALTER TABLE app_settings_scoped RENAME TO app_settings;
            """
        )


def _migration_ledger_lifecycle(conn: sqlite3.Connection) -> None:
    """Give the ledger the two things performance history cannot do without.

    ``transactions.external_id`` is a stable fingerprint of an imported row, so
    re-importing January's statement in February updates rather than duplicates.
    Without it "import the same statement twice" silently doubles someone's
    contributions and wrecks every return that depends on them.

    ``tax_lots`` gains a lifecycle. A lot used to be only its acquisition;
    there was nowhere to record that it was sold, for how much, or what was
    left. That is why a fully closed position could not keep its realized
    result — the schema had no place to put it.
    """
    transaction_cols = {row[1] for row in conn.execute("PRAGMA table_info(transactions)")}
    if "external_id" not in transaction_cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN external_id TEXT NOT NULL DEFAULT ''")
    # Partial index: '' means "entered by hand", and hand-entered rows must be
    # allowed to repeat — someone can genuinely buy the same thing twice in a
    # day. Only fingerprinted imports are held unique.
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_external
             ON transactions(user_id, external_id) WHERE external_id <> ''"""
    )

    lot_cols = {row[1] for row in conn.execute("PRAGMA table_info(tax_lots)")}
    if "remaining_quantity" not in lot_cols:
        conn.execute("ALTER TABLE tax_lots ADD COLUMN remaining_quantity REAL NOT NULL DEFAULT -1")
    # Backfill runs every time, not only when the column is added. -1 is the
    # "never filled in" sentinel, and on a shared deployment the owner adds the
    # column by hand — so the ALTER and the backfill do not necessarily happen
    # in the same place, or at all. Doing it unconditionally makes the step
    # self-healing instead of dependent on that ordering.
    conn.execute("UPDATE tax_lots SET remaining_quantity = quantity WHERE remaining_quantity < 0")
    if "disposed_at" not in lot_cols:
        conn.execute("ALTER TABLE tax_lots ADD COLUMN disposed_at TEXT NOT NULL DEFAULT ''")
    if "proceeds" not in lot_cols:
        conn.execute("ALTER TABLE tax_lots ADD COLUMN proceeds REAL NOT NULL DEFAULT 0")
    if "currency" not in lot_cols:
        conn.execute("ALTER TABLE tax_lots ADD COLUMN currency TEXT NOT NULL DEFAULT 'USD'")
    if "external_id" not in lot_cols:
        conn.execute("ALTER TABLE tax_lots ADD COLUMN external_id TEXT NOT NULL DEFAULT ''")
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_tax_lots_external
             ON tax_lots(user_id, external_id) WHERE external_id <> ''"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tax_lots_symbol ON tax_lots(user_id, symbol)")


MIGRATIONS: list[tuple[int, str, object]] = [
    (1, "baseline schema", _migration_baseline),
    (2, "briefings.trigger + emailed_at", _migration_briefing_columns),
    (3, "positions.source", _migration_position_source),
    (4, "positions.currency", _migration_position_currency),
    (5, "fundamentals cache", _migration_fundamentals),
    (6, "per-user scoping (user_id + composite uniqueness)", _migration_user_scope),
    (7, "shared quote cache + tracked symbols", _migration_shared_quotes),
    (8, "ledger dedupe + tax-lot lifecycle", _migration_ledger_lifecycle),
]


def schema_version() -> int:
    with connect() as conn:
        try:
            row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        except Exception:
            return 0  # table absent on a database that predates versioning
        return int((row["v"] if row else 0) or 0)


def init_db() -> None:
    """Bring the database to the current schema.

    SQLite replays the numbered migrations, because self-hosters have existing
    files to upgrade. Postgres is only ever created fresh for a shared
    deployment, so it is built at the current schema in one shot — with
    row-level security applied (see backend.dbschema_pg).
    """
    # Schema work belongs to no user, and runs at startup where no request is
    # in flight — so pin the neutral scope rather than asking a provider that
    # has nobody to name.
    with scope.using(scope.LOCAL_SCOPE):
        _init_db_locked()


def _init_db_locked() -> None:
    if dbdriver.is_postgres():
        from backend import dbschema_pg

        with connect() as conn:
            dbschema_pg.ensure(conn)
        return
    with connect() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_version (
                 version INTEGER PRIMARY KEY,
                 name TEXT NOT NULL,
                 applied_at TEXT NOT NULL
               )"""
        )
        applied = {row[0] for row in conn.execute("SELECT version FROM schema_version")}
        for version, name, migrate in MIGRATIONS:
            if version in applied:
                continue
            migrate(conn)
            conn.execute(
                "INSERT INTO schema_version (version, name, applied_at) VALUES (?, ?, ?)",
                (version, name, utcnow_iso()),
            )


def _create_baseline_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS positions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              symbol TEXT NOT NULL,
              name TEXT NOT NULL DEFAULT '',
              broker TEXT NOT NULL DEFAULT 'manual',
              asset_type TEXT NOT NULL DEFAULT 'stock',
              quantity REAL NOT NULL DEFAULT 0,
              average_cost REAL NOT NULL DEFAULT 0,
              current_price REAL NOT NULL DEFAULT 0,
              sector TEXT NOT NULL DEFAULT '',
              updated_at TEXT NOT NULL,
              UNIQUE(symbol, broker, asset_type)
            );
            CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol);
            CREATE INDEX IF NOT EXISTS idx_positions_broker ON positions(broker);

            CREATE TABLE IF NOT EXISTS tax_lots (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              symbol TEXT NOT NULL,
              broker TEXT NOT NULL DEFAULT 'manual',
              quantity REAL NOT NULL DEFAULT 0,
              cost_basis REAL NOT NULL DEFAULT 0,
              acquired_at TEXT NOT NULL,
              -- What is left of the lot, and what happened to the rest. A lot
              -- used to record only its purchase, which is why a sold-out
              -- position had nowhere to keep its realized result.
              remaining_quantity REAL NOT NULL DEFAULT 0,
              disposed_at TEXT NOT NULL DEFAULT '',
              proceeds REAL NOT NULL DEFAULT 0,
              currency TEXT NOT NULL DEFAULT 'USD',
              external_id TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tax_lots_symbol_broker ON tax_lots(symbol, broker);

            CREATE TABLE IF NOT EXISTS briefings (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              status TEXT NOT NULL,
              input_snapshot_json TEXT NOT NULL DEFAULT '{}',
              summary TEXT NOT NULL DEFAULT '',
              output_markdown TEXT NOT NULL DEFAULT '',
              model TEXT NOT NULL DEFAULT '',
              model_cost_usd REAL NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              completed_at TEXT,
              error TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_briefings_created ON briefings(created_at DESC);

            CREATE TABLE IF NOT EXISTS app_settings (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );

            -- v0.5: transactions log (BUY/SELL/DIVIDEND/FEE/CASH_IN/CASH_OUT)
            CREATE TABLE IF NOT EXISTS transactions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              symbol TEXT NOT NULL DEFAULT '',
              broker TEXT NOT NULL DEFAULT 'manual',
              asset_type TEXT NOT NULL DEFAULT 'stock',
              action TEXT NOT NULL DEFAULT 'buy',
              quantity REAL NOT NULL DEFAULT 0,
              price REAL NOT NULL DEFAULT 0,
              fee REAL NOT NULL DEFAULT 0,
              amount REAL NOT NULL DEFAULT 0,
              currency TEXT NOT NULL DEFAULT 'USD',
              occurred_at TEXT NOT NULL,
              notes TEXT NOT NULL DEFAULT '',
              source TEXT NOT NULL DEFAULT 'manual',
              -- Stable fingerprint of an imported row, so re-importing a
              -- statement updates instead of duplicating. Empty for anything
              -- entered by hand, which must stay free to repeat.
              external_id TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_transactions_symbol ON transactions(symbol);
            CREATE INDEX IF NOT EXISTS idx_transactions_occurred ON transactions(occurred_at DESC);
            CREATE INDEX IF NOT EXISTS idx_transactions_action ON transactions(action);

            -- v0.5: accounts entity (taxable / IRA / 401k / crypto / savings)
            CREATE TABLE IF NOT EXISTS accounts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL UNIQUE,
              kind TEXT NOT NULL DEFAULT 'taxable',
              broker TEXT NOT NULL DEFAULT 'manual',
              currency TEXT NOT NULL DEFAULT 'USD',
              created_at TEXT NOT NULL
            );

            -- v0.6: local cache of daily closes so the trend chart survives
            -- provider rate limits (e.g. FMP 429) instead of re-fetching every load.
            CREATE TABLE IF NOT EXISTS price_history (
              symbol TEXT NOT NULL,
              date TEXT NOT NULL,
              close REAL NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY (symbol, date)
            );
            CREATE INDEX IF NOT EXISTS idx_price_history_symbol_date
              ON price_history(symbol, date);

            -- v0.7: USD-based FX rates cache for multi-currency aggregation.
            CREATE TABLE IF NOT EXISTS fx_rates (
              quote TEXT PRIMARY KEY,
              rate REAL NOT NULL,
              updated_at TEXT NOT NULL
            );

            -- v0.9: shared quote cache. Prices used to be fetched per user and
            -- written onto their position rows, so a hundred people holding
            -- AAPL bought the same number a hundred times. Un-scoped for the
            -- same reason price_history is: one fetch serves everyone.
            CREATE TABLE IF NOT EXISTS quotes (
              symbol TEXT NOT NULL,
              asset_type TEXT NOT NULL DEFAULT 'stock',
              price REAL NOT NULL,
              sector TEXT NOT NULL DEFAULT '',
              updated_at TEXT NOT NULL,
              PRIMARY KEY (symbol, asset_type)
            );

            -- The set of symbols anyone holds, so one deployment-wide refresh
            -- knows what to fetch. Symbols only — no owner, no quantity — so
            -- it answers "what to price" without crossing the tenant boundary
            -- (Postgres RLS would refuse a cross-user read of positions, and
            -- rightly).
            CREATE TABLE IF NOT EXISTS tracked_symbols (
              symbol TEXT NOT NULL,
              asset_type TEXT NOT NULL DEFAULT 'stock',
              updated_at TEXT NOT NULL,
              PRIMARY KEY (symbol, asset_type)
            );
            """
    )


def _derive(
    position: PositionIn,
    position_id: int,
    updated_at: str,
    source: str = "manual",
    fx_factor: float = 1.0,
) -> Position:
    """Derive display fields. ``fx_factor`` converts the position's native
    currency into the display currency: aggregates (market_value / total_cost /
    unrealized_gain) come out display-currency, while average_cost and
    current_price stay native (labelled by ``currency`` in the UI)."""
    multiplier = 100 if position.asset_type == "option" else 1
    market_value = position.quantity * position.current_price * multiplier * fx_factor
    total_cost = position.quantity * position.average_cost * multiplier * fx_factor
    unrealized_gain = market_value - total_cost
    unrealized_gain_pct = (unrealized_gain / total_cost * 100) if total_cost else 0.0
    return Position(
        id=position_id,
        symbol=position.symbol,
        name=position.name or position.symbol,
        broker=position.broker,
        asset_type=position.asset_type,
        quantity=position.quantity,
        average_cost=position.average_cost,
        current_price=position.current_price,
        sector=position.sector,
        currency=position.currency,
        market_value=market_value,
        total_cost=total_cost,
        unrealized_gain=unrealized_gain,
        unrealized_gain_pct=unrealized_gain_pct,
        updated_at=updated_at,
        source=source,
    )


def _fx_factors(currencies: set[str]) -> dict[str, float]:
    """Per-currency multipliers into the display currency.

    Short-circuits to all-1.0 when every position already matches the display
    currency, so single-currency portfolios (the common case, and the test
    suite) never touch the FX cache or network.
    """
    from backend import fx  # lazy: fx imports db

    display = fx.display_currency()
    if all(code == display for code in currencies):
        return {code: 1.0 for code in currencies}
    rates = fx.get_rates()
    return {code: fx.convert_factor(code, display, rates) for code in currencies}


def _row_to_position(row: sqlite3.Row, fx_factor: float | None = None) -> Position:
    keys = row.keys()
    currency = row["currency"] if "currency" in keys else "USD"
    if fx_factor is None:
        fx_factor = _fx_factors({currency}).get(currency, 1.0)
    return _derive(
        PositionIn(
            symbol=row["symbol"],
            name=row["name"],
            broker=row["broker"],
            asset_type=row["asset_type"],
            quantity=row["quantity"],
            average_cost=row["average_cost"],
            current_price=row["current_price"],
            sector=row["sector"],
            currency=currency,
        ),
        row["id"],
        row["updated_at"],
        source=row["source"] if "source" in keys else "manual",
        fx_factor=fx_factor,
    )


def list_positions(include_closed: bool = False) -> list[Position]:
    """Current holdings. Closed positions are excluded unless asked for.

    A sold-out holding keeps its row so its lots, transactions and realized
    result stay in the performance history — but it is not a holding any more,
    and showing a row worth nothing on the dashboard is just clutter. History
    wants ``include_closed=True``; every screen that means "what do I own"
    wants the default.
    """
    closed_clause = "" if include_closed else " AND quantity <> 0"
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT * FROM positions WHERE user_id=?{closed_clause}
                 ORDER BY asset_type = 'cash', symbol, broker""",
            (scope.current(),),
        ).fetchall()
    currencies = {row["currency"] if "currency" in row.keys() else "USD" for row in rows}
    factors = _fx_factors(currencies) if rows else {}
    return [
        _row_to_position(row, factors.get(row["currency"] if "currency" in row.keys() else "USD", 1.0))
        for row in rows
    ]


def get_position(position_id: int) -> Position | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM positions WHERE id=? AND user_id=?", (position_id, scope.current())
        ).fetchone()
    return _row_to_position(row) if row else None


def create_position(position: PositionIn) -> Position:
    now = utcnow_iso()
    with connect() as conn:
        position_id = dbdriver.insert_returning_id(
            conn,
            """INSERT INTO positions
               (user_id, symbol, name, broker, asset_type, quantity, average_cost, current_price, sector, currency, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                scope.current(),
                position.symbol,
                position.name or position.symbol,
                position.broker,
                position.asset_type,
                position.quantity,
                position.average_cost,
                position.current_price,
                position.sector,
                position.currency,
                now,
            ),
        )
        _track_on(conn, position.symbol, position.asset_type)
    return _derive(position, position_id, now)


def upsert_position(position: PositionIn) -> Position:
    now = utcnow_iso()
    with connect() as conn:
        conn.execute(
            """INSERT INTO positions
               (user_id, symbol, name, broker, asset_type, quantity, average_cost, current_price, sector, currency, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, symbol, broker, asset_type) DO UPDATE SET
                 name=excluded.name,
                 quantity=excluded.quantity,
                 average_cost=excluded.average_cost,
                 current_price=excluded.current_price,
                 sector=excluded.sector,
                 currency=excluded.currency,
                 updated_at=excluded.updated_at""",
            (
                scope.current(),
                position.symbol,
                position.name or position.symbol,
                position.broker,
                position.asset_type,
                position.quantity,
                position.average_cost,
                position.current_price,
                position.sector,
                position.currency,
                now,
            ),
        )
        _track_on(conn, position.symbol, position.asset_type)
        row = conn.execute(
            """SELECT * FROM positions
               WHERE user_id=? AND symbol=? AND broker=? AND asset_type=?""",
            (scope.current(), position.symbol, position.broker, position.asset_type),
        ).fetchone()
    return _row_to_position(row)


def update_position(position_id: int, position: PositionIn) -> Position | None:
    now = utcnow_iso()
    with connect() as conn:
        cur = conn.execute(
            """UPDATE positions SET
                 symbol=?, name=?, broker=?, asset_type=?, quantity=?,
                 average_cost=?, current_price=?, sector=?, currency=?, updated_at=?
               WHERE id=? AND user_id=?""",
            (
                position.symbol,
                position.name or position.symbol,
                position.broker,
                position.asset_type,
                position.quantity,
                position.average_cost,
                position.current_price,
                position.sector,
                position.currency,
                now,
                position_id,
                scope.current(),
            ),
        )
        if cur.rowcount == 0:
            return None
        _track_on(conn, position.symbol, position.asset_type)
    return get_position(position_id)


def update_prices(prices: dict[str, tuple[float, str]]) -> int:
    now = utcnow_iso()
    count = 0
    with connect() as conn:
        for symbol, (price, sector) in prices.items():
            cur = conn.execute(
                """UPDATE positions
                   SET current_price=?, sector=COALESCE(NULLIF(?, ''), sector), updated_at=?
                   WHERE user_id=? AND symbol=? AND asset_type != 'cash'""",
                (price, sector, now, scope.current(), symbol.upper()),
            )
            count += cur.rowcount
    return count


def delete_position(position_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM positions WHERE id=? AND user_id=?", (position_id, scope.current())
        )
    return cur.rowcount > 0


# --- price-history cache --------------------------------------------------
# Persist daily closes locally so the trend chart can keep rendering when the
# upstream provider rate-limits us (e.g. FMP 429) instead of hammering it on
# every page load. Keyed by (symbol, date); newer fetches overwrite a day.


def cache_price_history(history: dict[str, dict]) -> int:
    """Upsert ``{symbol: {"dates": [...], "closes": [...]}}`` into the cache.

    Returns the number of (symbol, date) rows written.
    """
    now = utcnow_iso()
    written = 0
    with connect() as conn:
        for symbol, series in history.items():
            dates = series.get("dates") or []
            closes = series.get("closes") or []
            for day, close in zip(dates, closes, strict=False):
                day = str(day)[:10]
                if not day or close is None:
                    continue
                conn.execute(
                    """INSERT INTO price_history (symbol, date, close, updated_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(symbol, date)
                       DO UPDATE SET close=excluded.close, updated_at=excluded.updated_at""",
                    (symbol.upper(), day, float(close), now),
                )
                written += 1
    return written


# --- shared quote cache -----------------------------------------------------
# Quotes are the same number for everyone who holds the symbol, so they are
# fetched once per deployment rather than once per user. See the `quotes` and
# `tracked_symbols` DDL for why neither table carries a user_id.

_cache_warned = False


def _absent_table(exc: Exception) -> bool:
    """Whether ``exc`` is just "those tables aren't there yet".

    SQLite self-hosters get them from migration 7 at startup. Postgres does
    not: the app role has no DDL rights by design, so a shared deployment runs
    new code for however long it takes an operator to run
    ``python -m backend.dbschema_pg``. The cache is an optimisation — missing
    it has to cost speed, never function.
    """
    if type(exc).__name__ == "UndefinedTable":  # psycopg, without importing it
        return True
    return isinstance(exc, sqlite3.OperationalError) and "no such table" in str(exc).lower()


def _warn_cache_absent(exc: Exception) -> None:
    global _cache_warned
    if not _cache_warned:
        _cache_warned = True
        logger.warning(
            "Shared quote cache tables are missing (%s) — pricing falls back to "
            "per-user fetches. Run `python -m backend.dbschema_pg` as the database "
            "owner to enable it.",
            exc,
        )


def _track_on(conn, symbol: str, asset_type: str) -> None:
    """Add one symbol to the work-list, inside the caller's transaction.

    Every position write goes through here, so a symbol becomes priceable the
    moment someone holds it rather than at the next full scan.
    """
    symbol = (symbol or "").strip().upper()
    asset_type = (asset_type or "stock").strip().lower()
    if not symbol or asset_type in ("cash", "option"):
        return
    try:
        conn.execute(
            """INSERT INTO tracked_symbols (symbol, asset_type, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(symbol, asset_type)
               DO UPDATE SET updated_at=excluded.updated_at""",
            (symbol, asset_type, utcnow_iso()),
        )
    except Exception as exc:
        # This runs inside the caller's position write. Letting it raise would
        # roll back someone's holding to protect a cache.
        if not _absent_table(exc):
            raise
        _warn_cache_absent(exc)


def track_symbols(pairs: Iterable[tuple[str, str]]) -> int:
    """Record ``(symbol, asset_type)`` pairs the deployment needs priced.

    Called from every position write, so the refresher's work-list stays
    current without ever reading across users. Cash and options are skipped —
    nothing to quote.
    """
    now = utcnow_iso()
    written = 0
    try:
        with connect() as conn:
            for symbol, asset_type in pairs:
                symbol = (symbol or "").strip().upper()
                asset_type = (asset_type or "stock").strip().lower()
                if not symbol or asset_type in ("cash", "option"):
                    continue
                conn.execute(
                    """INSERT INTO tracked_symbols (symbol, asset_type, updated_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT(symbol, asset_type)
                       DO UPDATE SET updated_at=excluded.updated_at""",
                    (symbol, asset_type, now),
                )
                written += 1
    except Exception as exc:
        if not _absent_table(exc):
            raise
        _warn_cache_absent(exc)
        return 0
    return written


def list_tracked_symbols() -> list[tuple[str, str]]:
    """Every ``(symbol, asset_type)`` the deployment prices, for anyone."""
    try:
        with connect() as conn:
            rows = conn.execute(
                "SELECT symbol, asset_type FROM tracked_symbols ORDER BY symbol, asset_type"
            ).fetchall()
    except Exception as exc:
        if not _absent_table(exc):
            raise
        _warn_cache_absent(exc)
        return []
    return [(row["symbol"], row["asset_type"]) for row in rows]


def cache_quotes(rows: Iterable[tuple[str, str, float, str]]) -> int:
    """Upsert ``(symbol, asset_type, price, sector)`` quotes into the shared cache.

    One statement for the whole sweep, not one per symbol. Against Postgres
    each execute is a network round-trip, so a 500-symbol deployment pricing
    itself every minute would have spent 500 of them a sweep and 195,000 a
    day — the sweep would have taken longer than the interval it runs on, and
    the cost would have grown with the customer list.
    """
    now = utcnow_iso()
    payload = []
    for symbol, asset_type, price, sector in rows:
        if price is None or float(price) <= 0:
            continue
        payload.append((
            symbol.strip().upper(),
            (asset_type or "stock").strip().lower(),
            float(price),
            sector or "",
            now,
        ))
    if not payload:
        return 0
    written = 0
    try:
        with connect() as conn:
            conn.executemany(
                """INSERT INTO quotes (symbol, asset_type, price, sector, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(symbol, asset_type) DO UPDATE SET
                     price=excluded.price,
                     sector=CASE WHEN excluded.sector <> '' THEN excluded.sector ELSE quotes.sector END,
                     updated_at=excluded.updated_at""",
                payload,
            )
            written = len(payload)
    except Exception as exc:
        if not _absent_table(exc):
            raise
        _warn_cache_absent(exc)
        return 0
    return written


def get_cached_quotes(
    pairs: Iterable[tuple[str, str]] | None = None,
) -> dict[tuple[str, str], tuple[float, str, str]]:
    """``{(symbol, asset_type): (price, sector, updated_at)}`` from the cache.

    ``pairs`` filters to what a caller cares about; omit it for everything.
    Freshness is the caller's decision — the scheduler refetches regardless,
    a user-triggered refresh honours a window.
    """
    wanted = list(pairs) if pairs is not None else None
    if wanted is not None and not wanted:
        return {}
    sql = "SELECT symbol, asset_type, price, sector, updated_at FROM quotes"
    params: list[str] = []
    if wanted is not None:
        placeholders = ",".join("?" for _ in wanted)
        sql += f" WHERE symbol IN ({placeholders})"
        params = [symbol.strip().upper() for symbol, _ in wanted]
    try:
        with connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
    except Exception as exc:
        if not _absent_table(exc):
            raise
        _warn_cache_absent(exc)
        return {}  # every symbol reads as a miss; callers fetch as they used to
    return {
        (row["symbol"], row["asset_type"]): (row["price"], row["sector"], row["updated_at"])
        for row in rows
    }


def cached_history_bounds(symbols: list[str]) -> dict[str, dict[str, str]]:
    """``{SYMBOL: {"earliest": "YYYY-MM-DD", "latest": "YYYY-MM-DD"}}`` for cached
    history — lets callers fetch only the missing tail instead of re-pulling
    dates already stored. Cheap MIN/MAX aggregate."""
    if not symbols:
        return {}
    wanted = [s.upper() for s in symbols]
    placeholders = ",".join("?" for _ in wanted)
    out: dict[str, dict[str, str]] = {}
    with connect() as conn:
        for row in conn.execute(
            f"""SELECT symbol, MIN(date) AS earliest, MAX(date) AS latest
                FROM price_history WHERE symbol IN ({placeholders}) GROUP BY symbol""",
            tuple(wanted),
        ):
            if row["earliest"] and row["latest"]:
                out[row["symbol"]] = {"earliest": row["earliest"], "latest": row["latest"]}
    return out


def get_cached_price_history(symbols: list[str], start_date: str | None = None) -> dict[str, dict]:
    """Read cached daily closes for ``symbols`` (optionally on/after ``start_date``).

    Returns the same ``{symbol: {"dates": [...], "closes": [...]}}`` shape the
    providers emit, sorted ascending by date. Symbols with fewer than two cached
    points are omitted so callers treat them as a cache miss.
    """
    if not symbols:
        return {}
    wanted = [s.upper() for s in symbols]
    placeholders = ",".join("?" for _ in wanted)
    params: list[str] = list(wanted)
    clause = ""
    if start_date:
        clause = " AND date >= ?"
        params.append(start_date)
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT symbol, date, close FROM price_history
                WHERE symbol IN ({placeholders}){clause}
                ORDER BY symbol, date""",
            params,
        ).fetchall()
    grouped: dict[str, dict] = {}
    for row in rows:
        bucket = grouped.setdefault(row["symbol"], {"dates": [], "closes": []})
        bucket["dates"].append(row["date"])
        bucket["closes"].append(row["close"])
    return {sym: series for sym, series in grouped.items() if len(series["dates"]) >= 2}


def get_cached_fundamentals(symbols: list[str]) -> dict[str, dict]:
    """``{SYMBOL: {"payload": dict, "fetched_at": iso}}`` for cached fundamentals.

    Payloads are stored verbatim (including negative-cache misses, where
    ``payload["source"]`` is None) — freshness policy lives in
    ``backend.fundamentals``, not here.
    """
    if not symbols:
        return {}
    wanted = [s.upper() for s in symbols]
    placeholders = ",".join("?" for _ in wanted)
    out: dict[str, dict] = {}
    with connect() as conn:
        for row in conn.execute(
            f"SELECT symbol, payload, fetched_at FROM fundamentals WHERE symbol IN ({placeholders})",
            tuple(wanted),
        ):
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            out[row["symbol"]] = {"payload": payload, "fetched_at": row["fetched_at"]}
    return out


def upsert_fundamentals(symbol: str, payload: dict) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO fundamentals (symbol, payload, fetched_at) VALUES (?, ?, ?)
               ON CONFLICT(symbol)
               DO UPDATE SET payload=excluded.payload, fetched_at=excluded.fetched_at""",
            (symbol.upper(), json.dumps(payload), utcnow_iso()),
        )


#: Below this the two sources are describing the same holding. Brokers report
#: fractional shares from dividend reinvestment at more precision than a person
#: types, so an exact compare would call 387.128022 and 387.13 a disagreement.
_QUANTITY_TOLERANCE = 0.01


def _materially_different(entered: float, synced: float) -> bool:
    if entered == synced:
        return False
    # Relative for large holdings, absolute for small ones: a hundredth of a
    # share matters on a 2-share position and is noise on a 2,000-share one.
    return abs(entered - synced) > max(_QUANTITY_TOLERANCE, abs(entered) * 0.001)


def replace_synced_positions(
    rows: list[PositionIn], brokers: set[str], source: str = "snaptrade"
) -> dict:
    """Reconcile synced positions for the given brokers and source.

    Upserts every row tagged with ``source`` (e.g. 'snaptrade', 'coinbase'),
    then removes any previously synced rows for those brokers *and that source*
    that were not in this sync (sold/closed). Manual, CSV, and other-source
    positions are never touched. Scoping deletes to ``brokers`` + ``source``
    means syncing one connection never disturbs another's holdings.
    """
    now = utcnow_iso()
    seen: set[tuple[str, str, str]] = set()
    new_symbols: set[str] = set()
    conflicts: list[dict] = []
    with connect() as conn:
        existing_keys: set[tuple[str, str, str]] = set()
        # Quantity and source come along so the upsert below can notice it is
        # about to overwrite a hand-entered figure with a different one. After
        # the write that fact is unrecoverable — the row is UNIQUE on
        # (symbol, broker, asset_type), so there is no second row to compare
        # against and the disagreement simply disappears.
        existing_rows: dict[tuple[str, str, str], tuple[float, str]] = {}
        if brokers:
            placeholders = ",".join("?" for _ in brokers)
            for row in conn.execute(
                f"""SELECT symbol, broker, asset_type, quantity, source FROM positions
                    WHERE user_id=? AND broker IN ({placeholders})""",
                (scope.current(), *brokers),
            ):
                key = (row["symbol"], row["broker"], row["asset_type"])
                existing_keys.add(key)
                existing_rows[key] = (float(row["quantity"] or 0), row["source"] or "manual")
        for position in rows:
            key = (position.symbol, position.broker, position.asset_type)
            if key not in existing_keys and position.asset_type != "cash":
                new_symbols.add(position.symbol)
            prior = existing_rows.get(key)
            if (
                prior is not None
                and prior[1] != source
                and position.asset_type != "cash"
                and _materially_different(prior[0], position.quantity)
            ):
                # Recorded, not resolved. The broker is the better authority on
                # what is held, so its number still wins the write — but a
                # holding someone typed as 500 and the broker calls 400 is a
                # question only they can answer, and after this statement
                # nothing remembers there was one.
                conflicts.append({
                    "symbol": position.symbol,
                    "broker": position.broker,
                    "asset_type": position.asset_type,
                    "entered_quantity": prior[0],
                    "entered_source": prior[1],
                    "synced_quantity": position.quantity,
                })
            conn.execute(
                """INSERT INTO positions
                   (user_id, symbol, name, broker, asset_type, quantity, average_cost, current_price, sector, currency, source, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, symbol, broker, asset_type) DO UPDATE SET
                     name=excluded.name,
                     quantity=excluded.quantity,
                     average_cost=excluded.average_cost,
                     -- The sync source owns holdings; market data owns ongoing
                     -- price. Keep the existing market-data price; only adopt
                     -- the synced price when we have none yet, so a re-sync
                     -- never reverts a fresh quote.
                     current_price=CASE WHEN positions.current_price > 0
                                        THEN positions.current_price
                                        ELSE excluded.current_price END,
                     sector=CASE WHEN excluded.sector != '' THEN excluded.sector ELSE positions.sector END,
                     currency=excluded.currency,
                     source=excluded.source,
                     updated_at=excluded.updated_at""",
                (
                    scope.current(),
                    position.symbol,
                    position.name or position.symbol,
                    position.broker,
                    position.asset_type,
                    position.quantity,
                    position.average_cost,
                    position.current_price,
                    position.sector,
                    position.currency,
                    source,
                    now,
                ),
            )
            seen.add((position.symbol, position.broker, position.asset_type))
            _track_on(conn, position.symbol, position.asset_type)

        removed = 0
        if brokers:
            placeholders = ",".join("?" for _ in brokers)
            stale = conn.execute(
                f"""SELECT id, symbol, broker, asset_type FROM positions
                    WHERE user_id=? AND source=? AND broker IN ({placeholders})""",
                (scope.current(), source, *brokers),
            ).fetchall()
            for row in stale:
                if (row["symbol"], row["broker"], row["asset_type"]) not in seen:
                    # Closed, not deleted. A holding that vanishes from a sync
                    # was sold, and deleting the row takes its whole history
                    # with it — which is the survivor bias itself: last year's
                    # return silently rewrites to exclude everything you no
                    # longer own. Zero the quantity and keep the row, its lots
                    # and its transactions.
                    conn.execute(
                        """UPDATE positions
                              SET quantity=0, updated_at=?
                            WHERE id=? AND user_id=?""",
                        (utcnow_iso(), row["id"], scope.current()),
                    )
                    removed += 1
    # Replaced wholesale each sync rather than appended: if someone fixed the
    # position, the next sync simply does not re-record it, so the warning
    # clears itself and needs no acknowledgement flow to get stale.
    set_setting(SYNC_CONFLICTS_KEY, json.dumps({"at": now, "conflicts": conflicts}))
    return {
        "upserted": len(rows),
        "removed": removed,
        "new_symbols": sorted(new_symbols),
        "conflicts": conflicts,
    }


SYNC_CONFLICTS_KEY = "sync_quantity_conflicts"


def sync_conflicts() -> list[dict]:
    """Quantity disagreements the last sync overwrote. Empty when there were none."""
    raw = get_setting(SYNC_CONFLICTS_KEY)
    if not raw:
        return []
    try:
        return list(json.loads(raw).get("conflicts") or [])
    except (json.JSONDecodeError, AttributeError):
        return []


def delete_positions_for_brokers(brokers: set[str], source: str = "snaptrade") -> int:
    if not brokers:
        return 0
    placeholders = ",".join("?" for _ in brokers)
    with connect() as conn:
        cur = conn.execute(
            f"DELETE FROM positions WHERE user_id=? AND source=? AND broker IN ({placeholders})",
            (scope.current(), source, *brokers),
        )
    return cur.rowcount


def _parse_lot_date(value: str) -> date:
    text = value.strip()
    if not text:
        return datetime.now().date()
    if "T" in text:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    return date.fromisoformat(text[:10])


def _row_to_tax_lot(row: sqlite3.Row) -> TaxLot:
    position = get_position_for_lot(row["symbol"], row["broker"])
    current_price = position.current_price if position else row["cost_basis"]
    multiplier = 100 if position and position.asset_type == "option" else 1
    market_value = row["quantity"] * current_price * multiplier
    total_cost = row["quantity"] * row["cost_basis"] * multiplier
    unrealized_gain = market_value - total_cost
    acquired = _parse_lot_date(row["acquired_at"])
    long_term_date = acquired + timedelta(days=365)
    days_to_long_term = max(0, (long_term_date - datetime.now().date()).days)
    holding_period = "long-term" if days_to_long_term == 0 else "short-term"
    return TaxLot(
        id=row["id"],
        symbol=row["symbol"],
        broker=row["broker"],
        quantity=row["quantity"],
        cost_basis=row["cost_basis"],
        acquired_at=row["acquired_at"],
        created_at=row["created_at"],
        current_price=current_price,
        market_value=market_value,
        unrealized_gain=unrealized_gain,
        unrealized_gain_pct=(unrealized_gain / total_cost * 100) if total_cost else 0.0,
        holding_period=holding_period,
        days_to_long_term=days_to_long_term,
    )


def get_position_for_lot(symbol: str, broker: str) -> Position | None:
    with connect() as conn:
        row = conn.execute(
            """SELECT * FROM positions
               WHERE user_id=? AND symbol=? AND broker=?
               ORDER BY asset_type = 'cash', id LIMIT 1""",
            (scope.current(), symbol.upper(), broker),
        ).fetchone()
    return _row_to_position(row) if row else None


def list_tax_lots(symbol: str | None = None, broker: str | None = None) -> list[TaxLot]:
    clauses = ["user_id=?"]
    params: list[str] = [scope.current()]
    if symbol:
        clauses.append("symbol=?")
        params.append(symbol.upper())
    if broker:
        clauses.append("broker=?")
        params.append(broker.strip().lower().replace(" ", "_"))
    where = f"WHERE {' AND '.join(clauses)}"
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT * FROM tax_lots
                {where}
                ORDER BY acquired_at DESC, id DESC""",
            params,
        ).fetchall()
    return [_row_to_tax_lot(row) for row in rows]


def create_tax_lot(lot: TaxLotIn) -> TaxLot:
    now = utcnow_iso()
    with connect() as conn:
        lot_id = dbdriver.insert_returning_id(
            conn,
            """INSERT INTO tax_lots
               (user_id, symbol, broker, quantity, cost_basis, acquired_at,
                remaining_quantity, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            # A new lot is open, so all of it remains. Leaving this to the
            # column default would create every lot already sold — the whole
            # quantity gone, cost basis stranded, and nothing to value.
            (scope.current(), lot.symbol, lot.broker, lot.quantity, lot.cost_basis,
             lot.acquired_at, lot.quantity, now),
        )
        row = conn.execute(
            "SELECT * FROM tax_lots WHERE id=? AND user_id=?", (lot_id, scope.current())
        ).fetchone()
    return _row_to_tax_lot(row)


def delete_tax_lot(lot_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM tax_lots WHERE id=? AND user_id=?", (lot_id, scope.current())
        )
    return cur.rowcount > 0


def portfolio_summary() -> PortfolioSummary:
    positions = list_positions()
    summary = PortfolioSummary(positions=positions)
    summary.total_value = sum(p.market_value for p in positions)
    summary.total_cost = sum(p.total_cost for p in positions if p.asset_type != "cash")
    summary.total_gain = sum(p.unrealized_gain for p in positions if p.asset_type != "cash")
    summary.total_gain_pct = (
        summary.total_gain / summary.total_cost * 100 if summary.total_cost else 0.0
    )
    summary.cash_value = sum(p.market_value for p in positions if p.asset_type == "cash")
    summary.broker_breakdown = {}
    summary.sector_breakdown = {}
    for position in positions:
        summary.broker_breakdown[position.broker] = (
            summary.broker_breakdown.get(position.broker, 0.0) + position.market_value
        )
        sector = "Cash" if position.asset_type == "cash" else (position.sector or "Unknown")
        summary.sector_breakdown[sector] = summary.sector_breakdown.get(sector, 0.0) + position.market_value
    summary.top_positions = sorted(
        [p for p in positions if p.asset_type != "cash"],
        key=lambda item: item.market_value,
        reverse=True,
    )[:5]
    updated = [p.updated_at for p in positions]
    summary.last_refresh = max(updated) if updated else None
    return summary


def create_briefing(snapshot: dict, model: str, trigger: str = "manual") -> Briefing:
    now = utcnow_iso()
    with connect() as conn:
        briefing_id = dbdriver.insert_returning_id(
            conn,
            """INSERT INTO briefings
               (status, input_snapshot_json, model, trigger, created_at, user_id)
               VALUES ('running', ?, ?, ?, ?, ?)""",
            (json.dumps(snapshot, sort_keys=True, default=str), model, trigger, now,
             scope.current()),
        )
    return Briefing(id=briefing_id, status="running", model=model, trigger=trigger, created_at=now)


def finish_briefing(
    briefing_id: int,
    *,
    status: str,
    summary: str = "",
    output_markdown: str = "",
    model_cost_usd: float = 0.0,
    error: str = "",
) -> None:
    with connect() as conn:
        conn.execute(
            """UPDATE briefings SET
                 status=?, summary=?, output_markdown=?, model_cost_usd=?,
                 completed_at=?, error=?
               WHERE id=? AND user_id=?""",
            (
                status,
                summary,
                output_markdown,
                model_cost_usd,
                utcnow_iso(),
                error,
                briefing_id,
                scope.current(),
            ),
        )


def list_briefings(limit: int = 30) -> list[Briefing]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT id, status, summary, output_markdown, model, model_cost_usd,
                      trigger, emailed_at, created_at, completed_at, error
               FROM briefings WHERE user_id=? ORDER BY created_at DESC LIMIT ?""",
            (scope.current(), limit),
        ).fetchall()
    return [Briefing(**dict(row)) for row in rows]


def list_briefings_since(since_iso: str, trigger: str | None = None) -> list[Briefing]:
    clauses = ["user_id = ?", "created_at >= ?"]
    params: list[str] = [scope.current(), since_iso]
    if trigger:
        clauses.append("trigger = ?")
        params.append(trigger)
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT id, status, summary, output_markdown, model, model_cost_usd,
                       trigger, emailed_at, created_at, completed_at, error
                FROM briefings WHERE {' AND '.join(clauses)}
                ORDER BY created_at DESC""",
            params,
        ).fetchall()
    return [Briefing(**dict(row)) for row in rows]


def any_briefing_running() -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM briefings WHERE user_id=? AND status='running' LIMIT 1",
            (scope.current(),),
        ).fetchone()
    return row is not None


def delete_briefing(briefing_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM briefings WHERE id=? AND user_id=?", (briefing_id, scope.current())
        )
    return cur.rowcount > 0


def get_briefing(briefing_id: int) -> Briefing | None:
    with connect() as conn:
        row = conn.execute(
            """SELECT id, status, summary, output_markdown, model, model_cost_usd,
                      trigger, emailed_at, created_at, completed_at, error
               FROM briefings WHERE id=? AND user_id=?""",
            (briefing_id, scope.current()),
        ).fetchone()
    return Briefing(**dict(row)) if row else None


def mark_briefing_emailed(briefing_id: int) -> str:
    now = utcnow_iso()
    with connect() as conn:
        conn.execute(
            "UPDATE briefings SET emailed_at=? WHERE id=? AND user_id=?",
            (now, briefing_id, scope.current()),
        )
    return now


# --- the settings cache -----------------------------------------------------

# ``get_setting`` was the busiest query in the deployment by a wide margin —
# ~1.5M calls in a month, 97% of which found no row at all and handed back the
# caller's default. Each one was a connection and a round trip to learn that
# nothing had changed.
#
# Keyed by scope *and* key, which is not decoration. The connector-config cache
# in ``connectors.registry`` is safe to key on the bare key precisely because it
# only ever reads the fixed instance scope; this one sits in front of rows that
# belong to people, so a key that forgot the scope would serve one account's
# settings to another. Writes drop the entry immediately, so a save is visible
# to the very next read; the TTL only bounds how long a second machine could
# serve a stale value after the first one changed it.
_SETTINGS_TTL_SECONDS = 300.0
_settings_cache: dict[tuple[str, str], tuple[float, str | None]] = {}
_settings_lock = threading.Lock()


def forget_settings_cache() -> None:
    """Drop the settings cache — after a restore or a purge, and between tests."""
    with _settings_lock:
        _settings_cache.clear()


def get_setting(key: str, default: str = "") -> str:
    """This scope's value for ``key``, or ``default`` when there is no row.

    A missing row is cached as a miss rather than as ``default``: two callers
    may ask for the same key with different fallbacks, so what is worth
    remembering is the row's absence, not one caller's answer to it.
    """
    entry = (scope.current(), key)
    now = time.monotonic()
    with _settings_lock:
        hit = _settings_cache.get(entry)
        if hit is not None and now - hit[0] < _SETTINGS_TTL_SECONDS:
            return default if hit[1] is None else hit[1]
    with connect() as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE user_id=? AND key=?", entry
        ).fetchone()
    value = row["value"] if row else None
    with _settings_lock:
        _settings_cache[entry] = (now, value)
    return default if value is None else value


def set_setting(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO app_settings (user_id, key, value) VALUES (?, ?, ?)
               ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value""",
            (scope.current(), key, value),
        )
    # Dropped rather than written through: two threads writing the same key
    # would race to leave the loser's value behind in the cache while the
    # database holds the winner's. Re-reading once is cheaper than being wrong.
    with _settings_lock:
        _settings_cache.pop((scope.current(), key), None)


DEFAULT_SCHEDULE = {"enabled": False, "time": "07:30", "timezone": "local", "email_enabled": False}
DEFAULT_BRIEFING_PREFERENCES = {"style": "operator"}


def get_schedule() -> dict:
    raw = get_setting("briefing_schedule")
    if not raw:
        return dict(DEFAULT_SCHEDULE)
    try:
        stored = json.loads(raw)
    except json.JSONDecodeError:
        return dict(DEFAULT_SCHEDULE)
    return {**DEFAULT_SCHEDULE, **{k: stored[k] for k in DEFAULT_SCHEDULE if k in stored}}


def set_schedule(schedule: dict) -> dict:
    merged = {**DEFAULT_SCHEDULE, **{k: schedule[k] for k in DEFAULT_SCHEDULE if k in schedule}}
    set_setting("briefing_schedule", json.dumps(merged))
    return merged


def get_briefing_preferences() -> dict:
    raw = get_setting("briefing_preferences")
    if not raw:
        return dict(DEFAULT_BRIEFING_PREFERENCES)
    try:
        stored = json.loads(raw)
    except json.JSONDecodeError:
        return dict(DEFAULT_BRIEFING_PREFERENCES)
    return {
        **DEFAULT_BRIEFING_PREFERENCES,
        **{k: stored[k] for k in DEFAULT_BRIEFING_PREFERENCES if k in stored},
    }


def set_briefing_preferences(preferences: dict) -> dict:
    merged = {
        **DEFAULT_BRIEFING_PREFERENCES,
        **{k: preferences[k] for k in DEFAULT_BRIEFING_PREFERENCES if k in preferences},
    }
    set_setting("briefing_preferences", json.dumps(merged))
    return merged


# --- transactions ---------------------------------------------------------

# Sign conventions: amount is the net cash impact of the transaction.
# Buy / fee / cash_out are negative (cash leaves your account); sell, dividend,
# interest, cash_in are positive. Split/transfer carry no cash impact.
# Signed cash impact per action. Zero means the row moves no cash: a split
# changes share counts, a transfer between two tracked accounts leaves one and
# enters the other, and an adjustment is a correction to holdings rather than
# to the balance.
_AMOUNT_SIGN = {
    "buy": -1, "sell": +1,
    "dividend": +1, "interest": +1,
    "deposit": +1, "cash_in": +1,
    "withdrawal": -1, "cash_out": -1,
    "fee": -1, "tax": -1,
    "split": 0, "transfer": 0, "fx": 0, "adjustment": 0,
}

#: Actions whose cash amount is carried in ``price`` rather than qty × price.
_CASH_AMOUNT_ACTIONS = (
    "dividend", "interest", "deposit", "withdrawal",
    "cash_in", "cash_out", "fee", "tax",
)


#: An option contract covers 100 shares. Prices are quoted and stored per
#: share, so the cash a contract moves is quantity * price * 100. Positions
#: already applied this; transactions did not, and every option trade was
#: recorded moving a hundredth of the money it really moved — $217 for a
#: $21,700 purchase.
CONTRACT_MULTIPLIER = 100


def contract_multiplier(asset_type: str | None) -> int:
    return CONTRACT_MULTIPLIER if asset_type == "option" else 1


def _derive_amount(action: str, quantity: float, price: float, fee: float,
                   asset_type: str | None = None) -> float:
    sign = _AMOUNT_SIGN.get(action, 0)
    quantity = quantity * contract_multiplier(asset_type)
    gross = quantity * price if action in ("buy", "sell") else (price if price else (quantity * 0))
    if action in _CASH_AMOUNT_ACTIONS:
        # price field carries the cash amount for non-share transactions
        gross = price if price else quantity
    base = sign * gross
    if action == "buy":
        return base - fee
    if action == "sell":
        return base - fee
    return base


def repair_option_amounts(dry_run: bool = True) -> dict:
    """Restate option rows written before the contract multiplier existed.

    ``amount`` is what a row did to the cash balance, and for options it was
    computed as quantity * price with no multiplier — $217 recorded for a
    $21,700 purchase. Realized results read quantity and price directly and
    are corrected by the multiplier alone, but the reconstructed cash balance
    reads ``amount``, so rows already on record have to be restated.

    Recomputed rather than multiplied by a hundred, so a row that already
    carries a broker-supplied amount converges on the right answer instead of
    being scaled a second time.
    """
    fixed, delta = 0, 0.0
    rows = [t for t in list_transactions(limit=500_000)
            if (t.asset_type or "") == "option"]
    for t in rows:
        want = _derive_amount(t.action, t.quantity, t.price, t.fee, t.asset_type)
        if abs(want - float(t.amount or 0)) <= 0.005:
            continue
        fixed += 1
        delta += want - float(t.amount or 0)
        if not dry_run:
            with connect() as conn:
                conn.execute("UPDATE transactions SET amount = ? WHERE id = ?",
                             (round(want, 2), t.id))
    return {"dry_run": dry_run, "option_rows": len(rows),
            "restated": fixed, "cash_delta": round(delta, 2)}


def _row_to_transaction(row: sqlite3.Row) -> Transaction:
    return Transaction(
        id=row["id"],
        symbol=row["symbol"],
        broker=row["broker"],
        asset_type=row["asset_type"],
        action=row["action"],
        quantity=row["quantity"],
        price=row["price"],
        fee=row["fee"],
        amount=row["amount"],
        currency=row["currency"],
        occurred_at=row["occurred_at"],
        notes=row["notes"],
        source=row["source"],
        created_at=row["created_at"],
    )


def _transaction_filters(
    symbol: str | None = None,
    action: str | None = None,
    broker: str | None = None,
    since: str | None = None,
    until: str | None = None,
    source: str | None = None,
) -> tuple[str, list]:
    clauses, params = ["user_id = ?"], [scope.current()]
    if symbol:
        clauses.append("symbol = ?")
        params.append(symbol.upper())
    if action:
        clauses.append("action = ?")
        params.append(action)
    if broker:
        clauses.append("broker = ?")
        params.append(broker.strip().lower().replace(" ", "_"))
    if source:
        clauses.append("source = ?")
        params.append(source)
    # Compare on the date part only. occurred_at may carry a time component,
    # so a plain "<= 2026-08-20" would drop everything that happened that day.
    # substr beats an index here, but a personal ledger is thousands of rows,
    # not millions, and a filter nobody can reason about costs more.
    if since:
        clauses.append("substr(occurred_at, 1, 10) >= ?")
        params.append(since[:10])
    if until:
        clauses.append("substr(occurred_at, 1, 10) <= ?")
        params.append(until[:10])
    return f"WHERE {' AND '.join(clauses)}", params


def list_transactions(
    symbol: str | None = None,
    action: str | None = None,
    limit: int = 500,
    broker: str | None = None,
    since: str | None = None,
    until: str | None = None,
    source: str | None = None,
    offset: int = 0,
) -> list[Transaction]:
    where, params = _transaction_filters(symbol, action, broker, since, until, source)
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM transactions {where} "
            "ORDER BY occurred_at DESC, id DESC LIMIT ? OFFSET ?",
            (*params, limit, max(0, offset)),
        ).fetchall()
    return [_row_to_transaction(row) for row in rows]


def count_transactions(
    symbol: str | None = None,
    action: str | None = None,
    broker: str | None = None,
    since: str | None = None,
    until: str | None = None,
    source: str | None = None,
) -> int:
    """Total matching rows, so a paged view can say how many it is paging."""
    where, params = _transaction_filters(symbol, action, broker, since, until, source)
    with connect() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM transactions {where}", tuple(params)
        ).fetchone()
    return int(row["n"] if row else 0)


def transaction_facets() -> dict:
    """The symbols, brokers and sources actually present, for filter menus.

    Read from the data rather than the type: a ledger imported from one broker
    should not offer a dropdown of twelve others, and a symbol you have never
    traded is a filter that can only return nothing.
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT symbol, broker, source, action FROM transactions WHERE user_id = ?",
            (scope.current(),),
        ).fetchall()
    symbols, brokers, sources, actions = set(), set(), set(), set()
    for row in rows:
        if row["symbol"]:
            symbols.add(row["symbol"])
        if row["broker"]:
            brokers.add(row["broker"])
        if row["source"]:
            sources.add(row["source"])
        if row["action"]:
            actions.add(row["action"])
    return {
        "symbols": sorted(symbols),
        "brokers": sorted(brokers),
        "sources": sorted(sources),
        "actions": sorted(actions),
    }


def create_transaction(
    t: TransactionIn, source: str = "manual", external_id: str = ""
) -> Transaction | None:
    """Record a transaction. Returns None when ``external_id`` was already imported.

    The duplicate check is the database's partial unique index, not a lookup
    here: two concurrent imports of the same statement would both pass a
    read-then-write check and both insert. Hand-entered rows pass an empty
    external_id and are never deduplicated — buying the same thing twice in a
    day is ordinary.
    """
    now = utcnow_iso()
    amount = _derive_amount(t.action, t.quantity, t.price, t.fee, t.asset_type)
    try:
        with connect() as conn:
            txn_id = dbdriver.insert_returning_id(
                conn,
                """INSERT INTO transactions
                   (user_id, symbol, broker, asset_type, action, quantity, price, fee, amount,
                    currency, occurred_at, notes, source, external_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (scope.current(), t.symbol, t.broker, t.asset_type, t.action, t.quantity, t.price,
                 t.fee, amount, t.currency, t.occurred_at, t.notes, source, external_id, now),
            )
    except Exception:
        if not external_id:
            raise
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM transactions WHERE id = ? AND user_id = ?",
            (txn_id, scope.current()),
        ).fetchone()
    return _row_to_transaction(row)


def update_transaction(transaction_id: int, t: TransactionIn) -> Transaction | None:
    """Correct a recorded transaction. Returns None when it does not exist.

    ``amount`` is recomputed rather than carried over: it is derived from
    action, quantity, price and fee, and an edit that changed a buy to a sell
    while leaving the old signed cash impact behind would corrupt every return
    built on top of it. ``external_id`` is deliberately left alone — it
    identifies the source row this came from, and editing a misread value must
    not make the statement importable a second time.
    """
    with connect() as conn:
        cur = conn.execute(
            """UPDATE transactions
               SET symbol = ?, broker = ?, asset_type = ?, action = ?, quantity = ?,
                   price = ?, fee = ?, amount = ?, currency = ?, occurred_at = ?, notes = ?
               WHERE id = ? AND user_id = ?""",
            (t.symbol, t.broker, t.asset_type, t.action, t.quantity, t.price, t.fee,
             _derive_amount(t.action, t.quantity, t.price, t.fee, t.asset_type), t.currency,
             t.occurred_at, t.notes, transaction_id, scope.current()),
        )
        if cur.rowcount == 0:
            return None
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM transactions WHERE id = ? AND user_id = ?",
            (transaction_id, scope.current()),
        ).fetchone()
    return _row_to_transaction(row) if row else None


def delete_transaction(transaction_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM transactions WHERE id = ? AND user_id = ?",
            (transaction_id, scope.current()),
        )
    return cur.rowcount > 0


def transaction_summary() -> dict:
    """Per-symbol roll-up of realized P&L + dividends, derived from the log."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT symbol, action, SUM(quantity) AS qty, SUM(amount) AS total
               FROM transactions WHERE user_id = ? AND symbol != '' GROUP BY symbol, action""",
            (scope.current(),),
        ).fetchall()
    by_symbol: dict[str, dict] = {}
    for row in rows:
        bucket = by_symbol.setdefault(row["symbol"], {
            "symbol": row["symbol"],
            "buys": 0.0, "sells": 0.0, "dividends": 0.0,
            "shares_bought": 0.0, "shares_sold": 0.0,
        })
        action, total, qty = row["action"], float(row["total"] or 0), float(row["qty"] or 0)
        if action == "buy":
            bucket["buys"] += -total  # buys are negative amounts; flip to positive cost
            bucket["shares_bought"] += qty
        elif action == "sell":
            bucket["sells"] += total
            bucket["shares_sold"] += qty
        elif action == "dividend":
            bucket["dividends"] += total
    return {"by_symbol": list(by_symbol.values())}


# --- accounts ------------------------------------------------------------

def _row_to_account(row: sqlite3.Row) -> Account:
    return Account(
        id=row["id"], name=row["name"], kind=row["kind"], broker=row["broker"],
        currency=row["currency"], created_at=row["created_at"],
    )


def list_accounts(with_summary: bool = True) -> list[Account]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM accounts WHERE user_id = ? ORDER BY id", (scope.current(),)
        ).fetchall()
    accounts = [_row_to_account(row) for row in rows]
    if not with_summary:
        return accounts
    # Aggregate market value and cost via the broker label (back-compat: positions
    # carry broker, not account_id, so we link accounts to positions by broker).
    positions = list_positions()
    for account in accounts:
        matching = [p for p in positions if p.broker == account.broker]
        account.market_value = sum(p.market_value for p in matching)
        account.total_cost = sum(p.total_cost for p in matching if p.asset_type != "cash")
        account.cash_value = sum(p.market_value for p in matching if p.asset_type == "cash")
        account.position_count = sum(1 for p in matching if p.asset_type != "cash")
    return accounts


def create_account(account: AccountIn) -> Account:
    now = utcnow_iso()
    with connect() as conn:
        account_id = dbdriver.insert_returning_id(
            conn,
            """INSERT INTO accounts (user_id, name, kind, broker, currency, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (scope.current(), account.name, account.kind, account.broker, account.currency, now),
        )
        row = conn.execute(
            "SELECT * FROM accounts WHERE id = ? AND user_id = ?",
            (account_id, scope.current()),
        ).fetchone()
    return _row_to_account(row)


def delete_account(account_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM accounts WHERE id = ? AND user_id = ?",
            (account_id, scope.current()),
        )
    return cur.rowcount > 0


# Tables that carry a user_id, i.e. everything one account owns. Deliberately
# *not* the whole schema: quotes, price_history, fundamentals, fx_rates and
# tracked_symbols are a shared cache with no owner, and deleting one person's
# account must not evict market data every other account reads.
SCOPED_TABLES = ("positions", "tax_lots", "transactions", "briefings", "accounts", "app_settings")


def scoped_tables_present(conn) -> list[str]:
    """The scoped tables this database actually has.

    Discovered rather than assumed so that a purge cannot silently skip a
    table an older or newer schema is missing — and so that adding a scoped
    table without updating SCOPED_TABLES fails loudly in the audit below
    instead of quietly leaving that person's rows behind.
    """
    present = []
    for table in SCOPED_TABLES:
        try:
            conn.execute(f"SELECT 1 FROM {table} LIMIT 1")
            present.append(table)
        except Exception:
            continue
    return present


def purge_scope(scope_id: str) -> dict[str, int]:
    """Delete every row belonging to one account. Returns rows removed per table.

    Used by account deletion, where "delete" has to mean the data is gone
    rather than hidden — App Store guideline 5.1.1(v) and Play's data-deletion
    requirement both ask for the real thing.
    """
    scope_id = (scope_id or "").strip()
    if not scope_id:
        raise ValueError("purge_scope needs a scope id")
    removed: dict[str, int] = {}
    # Bind the session to the scope being deleted, the way every other query
    # here binds to the caller's. Postgres row-level security is what actually
    # decides which rows this statement can see, so a purge run under someone
    # else's scope would silently delete nothing; the WHERE clause is the
    # second lock, not the first.
    with scope.using(scope_id), connect() as conn:
        for table in scoped_tables_present(conn):
            cursor = conn.execute(f"DELETE FROM {table} WHERE user_id=?", (scope_id,))
            removed[table] = max(cursor.rowcount or 0, 0)
    # This deletes app_settings rows out from under the cache. Deletion has to
    # mean gone, so the cache cannot go on answering for the account.
    forget_settings_cache()
    return removed

from __future__ import annotations

import json
import sqlite3
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

DB_PATH: Path = settings.db_path


def set_db_path(path: Path) -> None:
    global DB_PATH
    DB_PATH = path


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


MIGRATIONS: list[tuple[int, str, object]] = [
    (1, "baseline schema", _migration_baseline),
    (2, "briefings.trigger + emailed_at", _migration_briefing_columns),
    (3, "positions.source", _migration_position_source),
    (4, "positions.currency", _migration_position_currency),
    (5, "fundamentals cache", _migration_fundamentals),
    (6, "per-user scoping (user_id + composite uniqueness)", _migration_user_scope),
    (7, "shared quote cache + tracked symbols", _migration_shared_quotes),
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


def list_positions() -> list[Position]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE user_id=? ORDER BY asset_type = 'cash', symbol, broker",
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


def _track_on(conn, symbol: str, asset_type: str) -> None:
    """Add one symbol to the work-list, inside the caller's transaction.

    Every position write goes through here, so a symbol becomes priceable the
    moment someone holds it rather than at the next full scan.
    """
    symbol = (symbol or "").strip().upper()
    asset_type = (asset_type or "stock").strip().lower()
    if not symbol or asset_type in ("cash", "option"):
        return
    conn.execute(
        """INSERT INTO tracked_symbols (symbol, asset_type, updated_at)
           VALUES (?, ?, ?)
           ON CONFLICT(symbol, asset_type)
           DO UPDATE SET updated_at=excluded.updated_at""",
        (symbol, asset_type, utcnow_iso()),
    )


def track_symbols(pairs: Iterable[tuple[str, str]]) -> int:
    """Record ``(symbol, asset_type)`` pairs the deployment needs priced.

    Called from every position write, so the refresher's work-list stays
    current without ever reading across users. Cash and options are skipped —
    nothing to quote.
    """
    now = utcnow_iso()
    written = 0
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
    return written


def list_tracked_symbols() -> list[tuple[str, str]]:
    """Every ``(symbol, asset_type)`` the deployment prices, for anyone."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT symbol, asset_type FROM tracked_symbols ORDER BY symbol, asset_type"
        ).fetchall()
    return [(row["symbol"], row["asset_type"]) for row in rows]


def cache_quotes(rows: Iterable[tuple[str, str, float, str]]) -> int:
    """Upsert ``(symbol, asset_type, price, sector)`` quotes into the shared cache."""
    now = utcnow_iso()
    written = 0
    with connect() as conn:
        for symbol, asset_type, price, sector in rows:
            if price is None or float(price) <= 0:
                continue
            conn.execute(
                """INSERT INTO quotes (symbol, asset_type, price, sector, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(symbol, asset_type) DO UPDATE SET
                     price=excluded.price,
                     sector=CASE WHEN excluded.sector <> '' THEN excluded.sector ELSE quotes.sector END,
                     updated_at=excluded.updated_at""",
                (symbol.strip().upper(), (asset_type or "stock").strip().lower(), float(price), sector or "", now),
            )
            written += 1
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
    with connect() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
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
    with connect() as conn:
        existing_keys: set[tuple[str, str, str]] = set()
        if brokers:
            placeholders = ",".join("?" for _ in brokers)
            for row in conn.execute(
                f"""SELECT symbol, broker, asset_type FROM positions
                    WHERE user_id=? AND broker IN ({placeholders})""",
                (scope.current(), *brokers),
            ):
                existing_keys.add((row["symbol"], row["broker"], row["asset_type"]))
        for position in rows:
            key = (position.symbol, position.broker, position.asset_type)
            if key not in existing_keys and position.asset_type != "cash":
                new_symbols.add(position.symbol)
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
                    conn.execute(
                        "DELETE FROM positions WHERE id=? AND user_id=?",
                        (row["id"], scope.current()),
                    )
                    removed += 1
    return {"upserted": len(rows), "removed": removed, "new_symbols": sorted(new_symbols)}


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
               (user_id, symbol, broker, quantity, cost_basis, acquired_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (scope.current(), lot.symbol, lot.broker, lot.quantity, lot.cost_basis,
             lot.acquired_at, now),
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


def get_setting(key: str, default: str = "") -> str:
    with connect() as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE user_id=? AND key=?", (scope.current(), key)
        ).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO app_settings (user_id, key, value) VALUES (?, ?, ?)
               ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value""",
            (scope.current(), key, value),
        )


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
_AMOUNT_SIGN = {
    "buy": -1, "sell": +1,
    "dividend": +1, "interest": +1, "cash_in": +1,
    "fee": -1, "cash_out": -1,
    "split": 0, "transfer": 0,
}


def _derive_amount(action: str, quantity: float, price: float, fee: float) -> float:
    sign = _AMOUNT_SIGN.get(action, 0)
    gross = quantity * price if action in ("buy", "sell") else (price if price else (quantity * 0))
    if action in ("dividend", "interest", "cash_in", "cash_out", "fee"):
        # price field carries the cash amount for non-share transactions
        gross = price if price else quantity
    base = sign * gross
    if action == "buy":
        return base - fee
    if action == "sell":
        return base - fee
    return base


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


def list_transactions(symbol: str | None = None, action: str | None = None, limit: int = 500) -> list[Transaction]:
    clauses, params = ["user_id = ?"], [scope.current()]
    if symbol:
        clauses.append("symbol = ?")
        params.append(symbol.upper())
    if action:
        clauses.append("action = ?")
        params.append(action)
    where = f"WHERE {' AND '.join(clauses)}"
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM transactions {where} ORDER BY occurred_at DESC, id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
    return [_row_to_transaction(row) for row in rows]


def create_transaction(t: TransactionIn, source: str = "manual") -> Transaction:
    now = utcnow_iso()
    amount = _derive_amount(t.action, t.quantity, t.price, t.fee)
    with connect() as conn:
        txn_id = dbdriver.insert_returning_id(
            conn,
            """INSERT INTO transactions
               (user_id, symbol, broker, asset_type, action, quantity, price, fee, amount,
                currency, occurred_at, notes, source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (scope.current(), t.symbol, t.broker, t.asset_type, t.action, t.quantity, t.price,
             t.fee, amount, t.currency, t.occurred_at, t.notes, source, now),
        )
        row = conn.execute(
            "SELECT * FROM transactions WHERE id = ? AND user_id = ?",
            (txn_id, scope.current()),
        ).fetchone()
    return _row_to_transaction(row)


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

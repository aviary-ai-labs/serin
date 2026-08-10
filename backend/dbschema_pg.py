"""Postgres schema for shared deployments — including the row-level security
that makes isolation a database guarantee rather than a coding convention.

SQLite carries a migration history because self-hosters have existing files to
upgrade. Postgres does not: a shared deployment is created fresh at the current
schema, so this is one idempotent bootstrap instead of a replay of six years of
ALTERs.

**Why RLS matters here.** Every query in ``db.py`` already filters by
``user_id``. RLS is the second line: a query that forgets returns *no* rows
rather than *everyone's*. Given the blast radius — one missed clause exposes
every customer's portfolio — one line of defence is not enough.

Two details that are easy to get wrong and silently fatal:

* ``FORCE ROW LEVEL SECURITY`` — without it the table **owner bypasses every
  policy**, and since the app connects as the owner, the policies would never
  run. Enabling RLS alone would look correct and protect nothing.
* ``current_setting('serin.user_id', true)`` returns NULL when unset, and
  ``user_id = NULL`` matches no rows — so an unbound session fails closed.
"""

from __future__ import annotations

from typing import Any

# Tables partitioned per user. Market-data caches are deliberately absent:
# identical for everyone, and scoping them would refetch each symbol per user.
SCOPED_TABLES = ("positions", "tax_lots", "transactions", "briefings", "accounts", "app_settings")

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
  id BIGSERIAL PRIMARY KEY,
  user_id TEXT NOT NULL DEFAULT 'local',
  symbol TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  broker TEXT NOT NULL DEFAULT 'manual',
  asset_type TEXT NOT NULL DEFAULT 'stock',
  quantity DOUBLE PRECISION NOT NULL DEFAULT 0,
  average_cost DOUBLE PRECISION NOT NULL DEFAULT 0,
  current_price DOUBLE PRECISION NOT NULL DEFAULT 0,
  sector TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'manual',
  currency TEXT NOT NULL DEFAULT 'USD',
  UNIQUE (user_id, symbol, broker, asset_type)
);
CREATE INDEX IF NOT EXISTS idx_positions_user ON positions(user_id);
CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol);

CREATE TABLE IF NOT EXISTS tax_lots (
  id BIGSERIAL PRIMARY KEY,
  user_id TEXT NOT NULL DEFAULT 'local',
  symbol TEXT NOT NULL,
  broker TEXT NOT NULL DEFAULT 'manual',
  quantity DOUBLE PRECISION NOT NULL DEFAULT 0,
  cost_basis DOUBLE PRECISION NOT NULL DEFAULT 0,
  acquired_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tax_lots_user ON tax_lots(user_id);

CREATE TABLE IF NOT EXISTS transactions (
  id BIGSERIAL PRIMARY KEY,
  user_id TEXT NOT NULL DEFAULT 'local',
  symbol TEXT NOT NULL DEFAULT '',
  broker TEXT NOT NULL DEFAULT 'manual',
  asset_type TEXT NOT NULL DEFAULT 'stock',
  action TEXT NOT NULL DEFAULT 'buy',
  quantity DOUBLE PRECISION NOT NULL DEFAULT 0,
  price DOUBLE PRECISION NOT NULL DEFAULT 0,
  fee DOUBLE PRECISION NOT NULL DEFAULT 0,
  amount DOUBLE PRECISION NOT NULL DEFAULT 0,
  currency TEXT NOT NULL DEFAULT 'USD',
  occurred_at TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'manual',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions(user_id);

CREATE TABLE IF NOT EXISTS briefings (
  id BIGSERIAL PRIMARY KEY,
  user_id TEXT NOT NULL DEFAULT 'local',
  status TEXT NOT NULL,
  input_snapshot_json TEXT NOT NULL DEFAULT '{}',
  summary TEXT NOT NULL DEFAULT '',
  output_markdown TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  model_cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  completed_at TEXT,
  error TEXT NOT NULL DEFAULT '',
  trigger TEXT NOT NULL DEFAULT 'manual',
  emailed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_briefings_user ON briefings(user_id);

CREATE TABLE IF NOT EXISTS accounts (
  id BIGSERIAL PRIMARY KEY,
  user_id TEXT NOT NULL DEFAULT 'local',
  name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'taxable',
  broker TEXT NOT NULL DEFAULT 'manual',
  currency TEXT NOT NULL DEFAULT 'USD',
  created_at TEXT NOT NULL,
  UNIQUE (user_id, name)
);
CREATE INDEX IF NOT EXISTS idx_accounts_user ON accounts(user_id);

CREATE TABLE IF NOT EXISTS app_settings (
  user_id TEXT NOT NULL DEFAULT 'local',
  key TEXT NOT NULL,
  value TEXT NOT NULL,
  PRIMARY KEY (user_id, key)
);

-- Shared market-data caches: one fetch serves every user.
CREATE TABLE IF NOT EXISTS price_history (
  symbol TEXT NOT NULL,
  date TEXT NOT NULL,
  close DOUBLE PRECISION NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS fundamentals (
  symbol TEXT PRIMARY KEY,
  payload TEXT NOT NULL,
  fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fx_rates (
  quote TEXT PRIMARY KEY,
  rate DOUBLE PRECISION NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quotes (
  symbol TEXT NOT NULL,
  asset_type TEXT NOT NULL DEFAULT 'stock',
  price DOUBLE PRECISION NOT NULL,
  sector TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL,
  PRIMARY KEY (symbol, asset_type)
);

-- Symbols only: what to price, never who holds it. Deliberately outside RLS
-- so one refresh can serve every user without reading anyone's positions.
CREATE TABLE IF NOT EXISTS tracked_symbols (
  symbol TEXT NOT NULL,
  asset_type TEXT NOT NULL DEFAULT 'stock',
  updated_at TEXT NOT NULL,
  PRIMARY KEY (symbol, asset_type)
);
"""


def _rls_for(table: str) -> str:
    """Enable, FORCE, and (re)create the scope policy for one table."""
    return f"""
ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS {table}_scope ON {table};
CREATE POLICY {table}_scope ON {table}
  FOR ALL
  USING (user_id = current_setting('serin.user_id', true))
  WITH CHECK (user_id = current_setting('serin.user_id', true));
"""


def assert_rls_can_apply(conn: Any) -> None:
    """Refuse to run as a role that Postgres exempts from row-level security.

    Superusers and ``BYPASSRLS`` roles skip every policy unconditionally. The
    tables still report RLS as enabled and forced, so the deployment *looks*
    protected while every user can read every other user's portfolio — the
    worst possible failure, because nothing about it is visible.

    Create a dedicated login role instead::

        CREATE ROLE serin_app LOGIN PASSWORD '…' NOSUPERUSER NOBYPASSRLS;
        GRANT USAGE ON SCHEMA public TO serin_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO serin_app;
        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO serin_app;

    Set ``SERIN_ALLOW_RLS_BYPASS=1`` to proceed anyway — only sane for local
    schema bootstrapping, never for a deployment serving more than one person.
    """
    import os

    if os.environ.get("SERIN_ALLOW_RLS_BYPASS", "").strip() == "1":
        return
    row = conn.execute(
        "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
    ).fetchone()
    if not row:
        return
    exempt = bool(row["rolsuper"]) or bool(row["rolbypassrls"])
    if exempt:
        raise RuntimeError(
            "Serin is connecting to Postgres as a role that bypasses row-level "
            "security (superuser or BYPASSRLS). Every user would be able to read "
            "every other user's data, with the schema still reporting RLS as "
            "enabled. Connect as a dedicated NOSUPERUSER NOBYPASSRLS role — see "
            "backend/dbschema_pg.py — or set SERIN_ALLOW_RLS_BYPASS=1 if this is "
            "a single-user bootstrap."
        )


_PROBE_GUC = "serin.pooling_probe"


def assert_session_state_persists(conn: Any) -> None:
    """Refuse to run behind a transaction-mode connection pooler.

    ``bind_scope`` sets ``serin.user_id`` once, when a connection is checked
    out, and every statement after it depends on that value still being there —
    it is what the policies read. A transaction-mode pooler breaks the
    assumption: it hands each transaction to whichever backend is free and
    resets session state in between. ``connect()`` runs autocommit, so *every
    statement is its own transaction* and the window is as small as it gets.

    Two ways it goes wrong, neither visible from the schema:

    * the setting is gone, ``current_setting`` returns NULL, no policy matches,
      and the app reads as though the database were empty;
    * the setting is still there from **another user's** request, and the
      policies serve their rows without complaint.

    The second is why this raises instead of warning, and why there is no
    override: a pooler that fails this check cannot isolate users, and no
    configuration of Serin changes that. Point ``SERIN_DATABASE_URL`` at a
    session-mode pooler or a direct connection instead.

    Supabase in particular offers both on the same host, one digit apart, and
    its dashboard suggests the wrong one first::

        …pooler.supabase.com:6543   transaction mode — unusable here
        …pooler.supabase.com:5432   session mode — correct
    """
    from urllib.parse import urlsplit

    from backend import dbdriver

    try:  # a password may legitimately contain ':' and digits — parse, don't scan
        port = urlsplit(dbdriver.database_url()).port
    except ValueError:  # unparseable port; let the driver report it properly
        port = None
    if port == 6543:
        raise RuntimeError(
            "SERIN_DATABASE_URL points at port 6543, which is Supabase's "
            "transaction-mode pooler. Session settings do not survive there, so "
            "the scope binding that row-level security depends on would be lost "
            "between statements — or read back as another user's. Use the "
            "session-mode pooler on port 5432 (same host) or a direct connection."
        )

    conn.execute("SELECT set_config(?, 'ok', false)", (_PROBE_GUC,))
    row = conn.execute("SELECT current_setting(?, true) AS v", (_PROBE_GUC,)).fetchone()
    if not row or row["v"] != "ok":
        raise RuntimeError(
            "A session setting made by one statement was gone by the next, which "
            "means this connection is being pooled in transaction mode "
            "(Supavisor on :6543, PgBouncer with pool_mode=transaction, or an "
            "equivalent proxy). Serin binds the current user to the session for "
            "row-level security to read, so isolation cannot hold here. Connect "
            "in session mode or directly."
        )


def _schema_exists(conn: Any) -> bool:
    row = conn.execute("SELECT to_regclass('public.positions') AS t").fetchone()
    return bool(row and row["t"])


def verify_rls(conn: Any) -> None:
    """Confirm every scoped table is both RLS-enabled and FORCEd.

    Run at startup on the app's own least-privilege connection, so a schema
    that drifted — a table restored from a dump without its policies, say —
    stops the deployment instead of quietly serving everyone everyone's data.
    """
    rows = conn.execute(
        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
        "WHERE relname = ANY(?)",
        (list(SCOPED_TABLES),),
    ).fetchall()
    seen = {r["relname"]: (r["relrowsecurity"], r["relforcerowsecurity"]) for r in rows}
    unprotected = [
        table for table in SCOPED_TABLES
        if not all(seen.get(table, (False, False)))
    ]
    if unprotected:
        raise RuntimeError(
            "Row-level security is not active on: " + ", ".join(unprotected)
            + ". Bootstrap the schema as the table owner (python -m backend.dbschema_pg) "
              "before serving users — without it, one missed WHERE clause exposes "
              "every user's data."
        )


def apply(conn: Any) -> None:
    """Create the schema and lock every scoped table behind RLS.

    Requires table ownership, so this is a **deploy step**, not something the
    running app does — the app connects as a least-privilege role that cannot
    (and should not) issue DDL.
    """
    conn.executescript(SCHEMA)
    for table in SCOPED_TABLES:
        conn.executescript(_rls_for(table))


def ensure(conn: Any) -> None:
    """Startup path: bootstrap if the database is empty, else verify.

    A fresh database gets the schema (whoever runs it needs ownership). An
    existing one is only checked — the app role has no DDL rights, and
    shouldn't. Either way the role is checked for RLS exemption, because a
    superuser connection would make all of this decorative, and the connection
    is checked for transaction-mode pooling, which would make it inoperative.
    """
    if not _schema_exists(conn):
        apply(conn)
    verify_rls(conn)
    assert_rls_can_apply(conn)
    assert_session_state_persists(conn)


if __name__ == "__main__":  # deploy step: python -m backend.dbschema_pg
    import os

    from backend import dbdriver

    os.environ.setdefault("SERIN_ALLOW_RLS_BYPASS", "1")  # bootstrap runs as owner
    with dbdriver.connect(None) as _conn:  # type: ignore[arg-type]
        apply(_conn)
        print("schema + row-level security applied")

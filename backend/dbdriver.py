"""Database driver seam — SQLite for self-host, Postgres for shared deploys.

Self-hosting stays a single file on your disk with no service to run, which is
most of why ``docker compose up`` is the whole install. A shared deployment
needs real concurrency and, more importantly, row-level security as a second
line of defence under the scoping in :mod:`backend.scope`.

The queries stay **raw SQL** — no ORM. Only four things actually differ, and
they are contained here:

* **placeholders** — SQLite writes ``?``, psycopg writes ``%s``
* **new row ids** — SQLite has ``cursor.lastrowid``, Postgres needs
  ``RETURNING id``
* **DDL dialect** — ``AUTOINCREMENT`` vs ``GENERATED … AS IDENTITY``, and
  ``PRAGMA`` is SQLite-only
* **connection cost** — opening a SQLite file is free, opening a Postgres
  connection is a TLS handshake, so the Postgres side is pooled

Select with ``SERIN_DATABASE_URL``: unset (or ``sqlite://``) keeps SQLite; a
``postgresql://…`` URL switches drivers. Nothing else in the codebase needs to
know which one is live.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# --- which driver? ---------------------------------------------------------


def database_url() -> str:
    return os.environ.get("SERIN_DATABASE_URL", "").strip()


def is_postgres() -> bool:
    return database_url().startswith(("postgres://", "postgresql://"))


# --- statement translation -------------------------------------------------

# Only rewrite placeholders outside string literals — a '?' inside quotes is
# data, not a parameter marker.
_LITERAL = re.compile(r"'(?:[^']|'')*'")


def to_pg(sql: str) -> str:
    """Rewrite a SQLite statement for psycopg: ``?`` → ``%s``, and escape any
    literal ``%`` so psycopg doesn't read it as a placeholder of its own."""
    out: list[str] = []
    last = 0
    for match in _LITERAL.finditer(sql):
        out.append(sql[last:match.start()].replace("%", "%%").replace("?", "%s"))
        out.append(match.group(0).replace("%", "%%"))
        last = match.end()
    out.append(sql[last:].replace("%", "%%").replace("?", "%s"))
    return "".join(out)


class _PgCursor:
    """Wraps a psycopg cursor so callers keep using the sqlite3 API they were
    written against: ``.fetchone()``, ``.fetchall()``, ``.rowcount``, and
    iteration, with rows addressable by column name."""

    def __init__(self, cur: Any) -> None:
        self._cur = cur

    def fetchone(self) -> Any:
        return self._cur.fetchone()

    def fetchall(self) -> list[Any]:
        return self._cur.fetchall()

    def __iter__(self) -> Iterator[Any]:
        return iter(self._cur)

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    @property
    def lastrowid(self) -> int:
        # Postgres has no implicit lastrowid; db.py routes inserts that need an
        # id through insert_returning_id() instead.
        raise NotImplementedError("use dbdriver.insert_returning_id() on Postgres")


class _PgConnection:
    """sqlite3-shaped facade over a psycopg connection."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def execute(self, sql: str, params: Any = ()) -> _PgCursor:
        cur = self._conn.cursor()
        cur.execute(to_pg(sql), tuple(params) if params else None)
        return _PgCursor(cur)

    def executemany(self, sql: str, seq_of_params: Any) -> _PgCursor:
        """The batched form, which db.cache_quotes needs and Postgres would
        otherwise refuse.

        Absent, a caller that batches falls over with AttributeError at
        runtime and only on Postgres — SQLite has always had this, so the
        quote sweep passed every local test and then failed on every sweep in
        production. Anything sqlite3.Connection offers and this does not is a
        bug waiting for the deployment that uses it.
        """
        cur = self._conn.cursor()
        rows = [tuple(params) for params in seq_of_params]
        if rows:
            cur.executemany(to_pg(sql), rows)
        return _PgCursor(cur)

    def executescript(self, script: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(script)

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


# --- the Postgres connection pool ------------------------------------------

# Every checkout used to open its own connection and close it again. On SQLite
# that is a file handle; on Postgres it is a TCP connect, a TLS handshake and a
# pgbouncer auth lookup — roughly 4 KB of egress to read one row. The scheduler
# wakes every 30s and each pass reads a couple of dozen settings, so a single
# idle account was opening ~72,000 connections a day and spending ~7.9 GB a
# month on handshakes against a 27 MB database. Pooling makes that a startup
# cost instead of a per-query one; nothing above this module changes.

_POOL: Any = None
_POOL_URL: str = ""
_POOL_LOCK = threading.Lock()

# Session mode holds one server-side backend per pooled connection, so this is
# a real reservation against the database's connection limit, not just a local
# cache. One process serving a web app and the scheduler needs very few.
_DEFAULT_POOL_MAX = 5


def _pool_max_size() -> int:
    try:
        return max(1, int(os.environ.get("SERIN_DB_POOL_MAX", "")))
    except ValueError:
        return _DEFAULT_POOL_MAX


def _clear_scope(conn: Any) -> None:
    """Unbind the scope before a connection goes back to the pool.

    ``db.connect`` rebinds on every checkout, so on the paths that exist today
    this is belt to that braces. It is still worth the round trip: the policies
    read ``serin.user_id``, and a pooled connection is the first thing here
    that can *outlive* a request. Without this, a future caller that reached a
    connection without binding would inherit whoever used it last and read
    their rows; with it, that caller reads nothing. Empty is as fail-closed as
    unset — no real ``user_id`` equals ``''``.
    """
    conn.execute("SELECT set_config('serin.user_id', '', false)")


def _get_pool() -> Any:
    """The process-wide pool for the current ``SERIN_DATABASE_URL``.

    Keyed on the URL: a test that repoints the env, or a deploy that moves the
    database, builds a new pool instead of quietly serving connections to the
    old one.
    """
    global _POOL, _POOL_URL
    url = database_url()
    with _POOL_LOCK:
        if _POOL is not None and _POOL_URL == url:
            return _POOL
        if _POOL is not None:
            _POOL.close()
            _POOL, _POOL_URL = None, ""
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        pool = ConnectionPool(
            url,
            kwargs={"row_factory": dict_row, "autocommit": True},
            min_size=1,
            max_size=_pool_max_size(),
            # Managed Postgres hangs up connections that idle too long, and a
            # pool is mostly idle by design. Check on checkout so a dead one is
            # replaced here rather than surfacing as a failed request, and cap
            # the lifetime so connections are recycled before that happens.
            check=ConnectionPool.check_connection,
            max_idle=300,
            max_lifetime=1800,
            reset=_clear_scope,
            timeout=30,
            name="serin",
            open=True,
        )
        _POOL, _POOL_URL = pool, url
        return pool


def close_pool() -> None:
    """Drop the pool and every connection in it (shutdown, and between tests)."""
    global _POOL, _POOL_URL
    with _POOL_LOCK:
        if _POOL is not None:
            _POOL.close()
        _POOL, _POOL_URL = None, ""


@contextmanager
def connect(sqlite_path: Path) -> Iterator[Any]:
    """Yield a connection with the sqlite3 surface, whichever driver is live."""
    if not is_postgres():
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(sqlite_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            yield conn
        finally:
            conn.close()
        return

    with _get_pool().connection() as raw:
        yield _PgConnection(raw)


def insert_returning_id(conn: Any, sql: str, params: Any) -> int:
    """Run an INSERT and return the new row's id, on either driver."""
    if is_postgres():
        cur = conn.execute(sql.rstrip().rstrip(";") + " RETURNING id", params)
        return int(cur.fetchone()["id"])
    return int(conn.execute(sql, params).lastrowid)


def bind_scope(conn: Any, scope_value: str) -> None:
    """Bind the current scope to the session so Postgres row-level security can
    enforce isolation itself.

    This is the belt to the code's braces: every query in ``db.py`` already
    filters by ``user_id``, but a future query that forgets will return **no**
    rows here instead of everyone's. On SQLite there is no equivalent, so the
    filters are the only line of defence — which is why self-host stays
    single-user.
    """
    if is_postgres():
        conn.execute("SELECT set_config('serin.user_id', ?, false)", (scope_value,))

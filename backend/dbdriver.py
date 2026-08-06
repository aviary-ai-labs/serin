"""Database driver seam — SQLite for self-host, Postgres for shared deploys.

Self-hosting stays a single file on your disk with no service to run, which is
most of why ``docker compose up`` is the whole install. A shared deployment
needs real concurrency and, more importantly, row-level security as a second
line of defence under the scoping in :mod:`backend.scope`.

The queries stay **raw SQL** — no ORM. Only three things actually differ, and
they are contained here:

* **placeholders** — SQLite writes ``?``, psycopg writes ``%s``
* **new row ids** — SQLite has ``cursor.lastrowid``, Postgres needs
  ``RETURNING id``
* **DDL dialect** — ``AUTOINCREMENT`` vs ``GENERATED … AS IDENTITY``, and
  ``PRAGMA`` is SQLite-only

Select with ``SERIN_DATABASE_URL``: unset (or ``sqlite://``) keeps SQLite; a
``postgresql://…`` URL switches drivers. Nothing else in the codebase needs to
know which one is live.
"""

from __future__ import annotations

import os
import re
import sqlite3
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

    def executescript(self, script: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(script)

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


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

    import psycopg
    from psycopg.rows import dict_row

    raw = psycopg.connect(database_url(), row_factory=dict_row, autocommit=True)
    try:
        yield _PgConnection(raw)
    finally:
        raw.close()


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

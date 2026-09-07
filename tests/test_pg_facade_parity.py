"""The Postgres facade has to keep up with the SQLite API the code is written against.

``backend/dbdriver._PgConnection`` is a sqlite3-shaped wrapper around psycopg,
so every call site in ``backend/`` is written against sqlite3's connection API
and silently gets this instead when a deployment runs on Postgres. Anything
sqlite3 offers that the facade does not is therefore not a missing feature —
it is a crash that only happens in production.

That is not hypothetical. ``cache_quotes`` was rewritten to batch its writes
with ``executemany``, which turned 500 round trips a sweep into one. SQLite has
had ``executemany`` since forever, so the whole test suite passed; Postgres got
``AttributeError: '_PgConnection' object has no attribute 'executemany'`` and
every quote sweep in production failed until it was noticed in the logs.

So this checks the shape rather than any one method: whatever ``backend/``
calls on a connection, the facade must implement.
"""

from __future__ import annotations

import pathlib
import re
import sqlite3

from backend.dbdriver import _PgConnection, _PgCursor

BACKEND = pathlib.Path(__file__).resolve().parent.parent / "backend"

#: Names that look like connection calls but are not — narrow, and each one
#: earns its place by being something a connection genuinely does not own.
NOT_CONNECTION_METHODS = {"cursor", "row_factory", "closed", "info"}


def _db_connection_calls() -> set[str]:
    """Methods called on a real database connection anywhere in ``backend/``.

    Scoped to files that actually open one. ``conn`` is a popular local name —
    a market-data *connector* is bound to it in places — and scanning every
    file for ``conn.something()`` reports those as missing from a database
    facade, which is noise that would get this test deleted rather than fixed.
    """
    found: set[str] = set()
    for path in BACKEND.rglob("*.py"):
        text = path.read_text()
        if "connect() as conn" not in text:
            continue
        found.update(re.findall(r"\bconn\.([a-z_]+)\s*\(", text))
    return found - NOT_CONNECTION_METHODS


def test_the_facade_implements_every_connection_call_the_backend_makes():
    """The regression that broke every sweep in production, generalised."""
    called = _db_connection_calls()
    missing = sorted(name for name in called if not hasattr(_PgConnection, name))
    assert not missing, (
        f"backend/ calls conn.{{{', '.join(missing)}}} but the Postgres facade "
        f"has no such method — this works on SQLite and crashes on Postgres"
    )


def test_the_facade_covers_the_sqlite_methods_it_claims_to_stand_in_for():
    """A narrower belt: the connection API the facade advertises by existing.
    Listed explicitly so that adding one to the code without adding it here is
    a deliberate act rather than an oversight."""
    for name in ("execute", "executemany", "executescript", "commit", "close"):
        assert hasattr(sqlite3.Connection, name), f"sqlite3 lost {name}?"
        assert hasattr(_PgConnection, name), f"the Postgres facade is missing {name}"


def test_executemany_converts_placeholders_and_passes_every_row():
    """sqlite3 takes ``?``; psycopg takes ``%s``. The batched path has to go
    through the same conversion the single one does, or it fails on syntax
    rather than on the missing attribute."""
    calls = []

    class Cursor:
        def executemany(self, sql, rows): calls.append((sql, list(rows)))
        def execute(self, sql, params=None): calls.append((sql, params))

    class Conn:
        def cursor(self): return Cursor()

    result = _PgConnection(Conn()).executemany(
        "INSERT INTO quotes (symbol, price) VALUES (?, ?)",
        [("AAPL", 1.0), ("MSFT", 2.0)],
    )
    assert isinstance(result, _PgCursor)
    sql, rows = calls[0]
    assert "?" not in sql and "%s" in sql, f"placeholders not converted: {sql}"
    assert rows == [("AAPL", 1.0), ("MSFT", 2.0)]


def test_an_empty_batch_touches_the_database_not_at_all():
    """A sweep that priced nothing should not issue a statement with no rows."""
    calls = []

    class Cursor:
        def executemany(self, sql, rows): calls.append(sql)

    class Conn:
        def cursor(self): return Cursor()

    _PgConnection(Conn()).executemany("INSERT INTO quotes VALUES (?)", [])
    assert calls == []

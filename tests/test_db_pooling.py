"""The connection pool — why queries stopped paying for their own handshakes.

Unpooled, every ``db.connect()`` opened a Postgres connection and dropped it:
a TCP connect, a TLS handshake and a pgbouncer auth lookup, roughly 4 KB of
egress, to read one row. The scheduler wakes every 30s and each pass reads a
couple of dozen settings, so one idle account managed ~1.9M connections and
~7.9 GB of egress in a month against a 27 MB database.

What is under test is that ``connect`` now *reuses* connections, and that
reuse cannot leak one user's scope into another's request. These drive a stub
pool so they hold on SQLite CI, with no Postgres anywhere.
"""

from __future__ import annotations

from contextlib import contextmanager

import psycopg_pool
import pytest
from backend import dbdriver

SESSION_MODE = "postgresql://serin_app:pw@aws-0-us-east-1.pooler.supabase.com:5432/postgres"
OTHER_DATABASE = "postgresql://serin_app:pw@aws-0-eu-west-1.pooler.supabase.com:5432/postgres"


class _FakeRaw:
    """Stands in for a psycopg connection; records what was run on it."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, sql: str, params: tuple = ()):
        self.statements.append(sql)
        return self

    def cursor(self):  # pragma: no cover - _PgConnection wants one, tests don't
        raise AssertionError("these tests do not run queries")


class _FakePool:
    """Records construction and checkouts so the tests can count both."""

    built: list[_FakePool] = []

    def __init__(self, conninfo: str, **kwargs) -> None:
        self.conninfo = conninfo
        self.kwargs = kwargs
        self.closed = False
        self.checkouts = 0
        self.conn = _FakeRaw()
        _FakePool.built.append(self)

    @staticmethod
    def check_connection(conn) -> None:  # the real pool exposes this as a class attr
        pass

    @contextmanager
    def connection(self):
        self.checkouts += 1
        yield self.conn

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def pooled(monkeypatch):
    """A live Postgres config with the pool class swapped for the stub."""
    _FakePool.built.clear()
    dbdriver.close_pool()
    monkeypatch.setenv("SERIN_DATABASE_URL", SESSION_MODE)
    monkeypatch.setattr(psycopg_pool, "ConnectionPool", _FakePool)
    yield _FakePool.built
    dbdriver.close_pool()


def test_repeated_connects_share_one_pool(pooled):
    """The regression this exists for.

    Three ``connect()`` calls used to mean three handshakes. They must now mean
    one pool, built once, checked out three times.
    """
    for _ in range(3):
        with dbdriver.connect(None):  # type: ignore[arg-type]
            pass

    assert len(pooled) == 1
    assert pooled[0].checkouts == 3


def test_the_pool_keeps_the_row_and_commit_behaviour_connect_had(pooled):
    """Pooling is meant to be invisible above this module: callers still get
    dict rows, and still get autocommit rather than an implicit transaction
    left open across a checkout."""
    with dbdriver.connect(None):  # type: ignore[arg-type]
        pass

    kwargs = pooled[0].kwargs["kwargs"]
    assert kwargs["autocommit"] is True
    assert kwargs["row_factory"].__name__ == "dict_row"


def test_returning_a_connection_unbinds_the_scope():
    """A pooled connection is the first thing here that outlives a request.

    The policies read ``serin.user_id``; a connection carrying a stale one into
    a path that forgot to bind would serve that user's rows. Cleared, it serves
    none — ``''`` matches no real ``user_id``.
    """
    conn = _FakeRaw()

    dbdriver._clear_scope(conn)

    assert conn.statements == ["SELECT set_config('serin.user_id', '', false)"]


def test_the_pool_is_wired_to_unbind_on_return(pooled):
    """Wiring, not logic: the reset above only protects anything if the pool
    is actually told to run it."""
    with dbdriver.connect(None):  # type: ignore[arg-type]
        pass

    assert pooled[0].kwargs["reset"] is dbdriver._clear_scope


def test_repointing_the_database_builds_a_new_pool(pooled, monkeypatch):
    """Connections are to a specific database. If the URL moves, the old pool
    must be closed rather than left serving the previous one."""
    with dbdriver.connect(None):  # type: ignore[arg-type]
        pass
    monkeypatch.setenv("SERIN_DATABASE_URL", OTHER_DATABASE)
    with dbdriver.connect(None):  # type: ignore[arg-type]
        pass

    assert [p.conninfo for p in pooled] == [SESSION_MODE, OTHER_DATABASE]
    assert pooled[0].closed is True


def test_sqlite_never_builds_a_pool(monkeypatch, tmp_path):
    """Self-host opens a file, which costs nothing. Nothing to pool, and
    importing the pool there would be a dependency self-hosters don't have."""
    _FakePool.built.clear()
    dbdriver.close_pool()
    monkeypatch.delenv("SERIN_DATABASE_URL", raising=False)

    with dbdriver.connect(tmp_path / "serin.db") as conn:
        conn.execute("SELECT 1")

    assert _FakePool.built == []


def test_pool_size_defaults_and_can_be_overridden(monkeypatch):
    """Session mode reserves a real backend per pooled connection, so the
    default stays small; a bigger deployment can say so."""
    monkeypatch.delenv("SERIN_DB_POOL_MAX", raising=False)
    assert dbdriver._pool_max_size() == 5

    monkeypatch.setenv("SERIN_DB_POOL_MAX", "20")
    assert dbdriver._pool_max_size() == 20


def test_an_unreadable_pool_size_falls_back_rather_than_crashing(monkeypatch):
    """A typo in an env var should not stop the process from serving."""
    monkeypatch.setenv("SERIN_DB_POOL_MAX", "lots")
    assert dbdriver._pool_max_size() == 5

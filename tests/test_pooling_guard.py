"""The pooling guard — the one isolation failure RLS cannot catch itself.

The policies read ``serin.user_id``, a *session* setting that ``bind_scope``
writes once per checked-out connection. Behind a transaction-mode pooler that
setting is reset before the next statement runs, so the policies either match
nothing or — if the backend is handed on mid-flight — match someone else's
rows. Throughout, ``pg_class`` still reports RLS as enabled and forced, which
is exactly why nothing downstream notices.

These run against a stub connection so they hold on SQLite CI: what's under
test is the guard's own logic, not Postgres'.
"""

from __future__ import annotations

import pytest
from backend import dbschema_pg

SESSION_MODE = "postgresql://serin_app:pw@aws-0-us-east-1.pooler.supabase.com:5432/postgres"
TRANSACTION_MODE = "postgresql://serin_app:pw@aws-0-us-east-1.pooler.supabase.com:6543/postgres"


class _FakeConn:
    """Just enough of ``execute(...).fetchone()`` to drive the guard.

    ``session_state_survives=False`` reproduces what a transaction pooler does
    between statements: return the backend to the pool, resetting whatever the
    last transaction set.
    """

    def __init__(self, *, session_state_survives: bool) -> None:
        self._survives = session_state_survives
        self._set: dict[str, str] = {}
        self._last: dict | None = None
        self.statements: list[str] = []

    def execute(self, sql: str, params: tuple = ()):
        self.statements.append(sql)
        if "set_config" in sql:
            self._set[params[0]] = "ok"
        elif "current_setting" in sql:
            if not self._survives:
                self._set.clear()
            self._last = {"v": self._set.get(params[0])}
        else:  # pragma: no cover - a new statement here wants a deliberate test
            raise AssertionError(f"unexpected statement: {sql}")
        return self

    def fetchone(self):
        return self._last


@pytest.fixture
def session_url(monkeypatch):
    monkeypatch.setenv("SERIN_DATABASE_URL", SESSION_MODE)


def test_session_mode_connection_is_accepted(session_url):
    conn = _FakeConn(session_state_survives=True)
    dbschema_pg.assert_session_state_persists(conn)  # does not raise


def test_lost_session_state_is_refused(session_url):
    """The general case: any proxy that resets between statements, whatever
    its name or port."""
    conn = _FakeConn(session_state_survives=False)
    with pytest.raises(RuntimeError, match="transaction mode"):
        dbschema_pg.assert_session_state_persists(conn)


def test_supabase_transaction_port_is_refused_without_probing(monkeypatch):
    """Port 6543 is rejected on the connection string alone.

    The probe can only observe a pooler that has actually recycled the
    connection, and at startup — one client, no contention — it may well hand
    back the same backend and look fine. The port is the deterministic signal,
    so it is checked first and the connection is never even used.
    """
    monkeypatch.setenv("SERIN_DATABASE_URL", TRANSACTION_MODE)
    conn = _FakeConn(session_state_survives=True)
    with pytest.raises(RuntimeError, match="6543"):
        dbschema_pg.assert_session_state_persists(conn)
    assert conn.statements == []


def test_transaction_port_is_caught_without_credentials(monkeypatch):
    """No userinfo in the URL, so there is no '@' to split on. Parsing the port
    properly is what makes this case work."""
    monkeypatch.setenv("SERIN_DATABASE_URL", "postgresql://localhost:6543/postgres")
    with pytest.raises(RuntimeError, match="6543"):
        dbschema_pg.assert_session_state_persists(_FakeConn(session_state_survives=True))


def test_6543_inside_the_password_is_not_the_pooler(monkeypatch):
    """Only the host's port counts. A credential that happens to contain the
    digits must not lock a correctly-configured deployment out of its own
    database."""
    monkeypatch.setenv(
        "SERIN_DATABASE_URL",
        "postgresql://serin_app:hunter:6543@aws-0-us-east-1.pooler.supabase.com:5432/postgres",
    )
    dbschema_pg.assert_session_state_persists(_FakeConn(session_state_survives=True))


def test_ensure_runs_the_guard(monkeypatch, session_url):
    """Wiring, not logic: a guard nobody calls protects nothing."""
    called: list[str] = []
    monkeypatch.setattr(dbschema_pg, "_schema_exists", lambda conn: True)
    monkeypatch.setattr(dbschema_pg, "verify_rls", lambda conn: None)
    monkeypatch.setattr(dbschema_pg, "assert_rls_can_apply", lambda conn: None)
    monkeypatch.setattr(
        dbschema_pg, "assert_session_state_persists", lambda conn: called.append("guard")
    )

    dbschema_pg.ensure(object())

    assert called == ["guard"]

"""A deployment that meant to have accounts must not serve without them.

Plugins are loaded best-effort: one that raises is logged and skipped so a
broken community connector cannot take the app down. That is right until the
plugin in question is the one bringing identity — then skipping it removes the
authorizer *and* the scope provider, and because a shared deployment has no
SERIN_AUTH_PASSWORD either, ``auth_enabled()`` goes false and every /api
request is served to anyone, against one pooled scope.

It has happened twice: an absolute import inside the pack, and a CREATE TABLE
the app role was not permitted to run. Both times the container came up
healthy, passed its health check, and served the landing page.
"""

from __future__ import annotations

import pytest
from backend import auth, scope
from backend.main import _assert_multiuser_intact


@pytest.fixture(autouse=True)
def clean_seams():
    """Leave the process as we found it — these are module-level globals."""
    yield
    auth.set_authorizer(None)
    scope.set_scope_provider(None)


def _install_both():
    auth.set_authorizer(lambda headers, cookies: True)
    scope.set_scope_provider(lambda: "someone")


def test_single_user_deployment_is_never_blocked(monkeypatch):
    """Self-host declares nothing and must keep booting with no seams at all."""
    monkeypatch.delenv("SERIN_MULTIUSER", raising=False)
    _assert_multiuser_intact()  # does not raise


def test_refuses_to_start_when_the_pack_did_not_load(monkeypatch):
    """The exact shape of both production failures: flag set, nothing installed."""
    monkeypatch.setenv("SERIN_MULTIUSER", "1")
    with pytest.raises(RuntimeError, match="authorizer"):
        _assert_multiuser_intact()


def test_refuses_when_only_the_scope_provider_is_missing(monkeypatch):
    """Half-installed is worse than not installed: requests authenticate, then
    every one of them resolves to whatever scope core falls back to."""
    monkeypatch.setenv("SERIN_MULTIUSER", "1")
    auth.set_authorizer(lambda headers, cookies: True)
    with pytest.raises(RuntimeError, match="scope provider"):
        _assert_multiuser_intact()


def test_refuses_when_only_the_authorizer_is_missing(monkeypatch):
    monkeypatch.setenv("SERIN_MULTIUSER", "1")
    scope.set_scope_provider(lambda: "someone")
    with pytest.raises(RuntimeError, match="authorizer"):
        _assert_multiuser_intact()


def test_starts_when_both_seams_are_filled(monkeypatch):
    monkeypatch.setenv("SERIN_MULTIUSER", "1")
    _install_both()
    _assert_multiuser_intact()  # does not raise


def test_the_error_names_the_thing_to_go_look_at(monkeypatch):
    """An operator reading this at 2am should not have to guess."""
    monkeypatch.setenv("SERIN_MULTIUSER", "1")
    with pytest.raises(RuntimeError) as caught:
        _assert_multiuser_intact()
    message = str(caught.value)
    assert "failed to load" in message  # points at the plugin traceback
    assert "pool" in message.lower()    # says what would have happened

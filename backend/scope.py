"""Data scope — the neutral seam that lets one deployment serve many users.

The open-source core is single-user: with no provider installed every call
runs as one implicit owner (:data:`LOCAL_SCOPE`), so a self-hoster never grows
accounts, a login screen, or a notion of "other people's rows". That is the
whole free tier, unchanged.

A commercial pack may install a provider with :func:`set_scope_provider`,
typically reading the authenticated user out of the request context. Core then
partitions every per-user table by whatever the provider returns, without
knowing what a "user" is.

**This seam fails closed, unlike** ``backend.entitlements``. A broken
entitlement verifier degrades to open-source behaviour, which is safe. A
broken *scope* provider cannot degrade to :data:`LOCAL_SCOPE` — that would
silently pool every user into one shared dataset and hand each of them
everyone else's portfolio. So an installed-but-failing provider raises.

Market-data caches (``price_history``, ``fundamentals``, ``fx_rates``) are
deliberately **not** scoped: the data is identical for every user, and
partitioning it would refetch each symbol once per user.
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable, Iterator
from contextlib import contextmanager

# The single implicit owner when nobody has installed a provider. Also the
# value stored in every row of a self-hosted database, so a single-user
# instance that later joins a shared deployment has a coherent scope already.
LOCAL_SCOPE = "local"

# Not everything in app_settings belongs to a person. Connector enablement and
# the operator's encrypted provider keys are properties of the *deployment*:
# in Cloud one SnapTrade key serves every user, and a background task
# encrypting secrets at startup has no user to act as. Those reads pin this
# scope. On self-host it is simply another row alongside LOCAL_SCOPE.
INSTANCE_SCOPE = "instance"


class ScopeError(RuntimeError):
    """A provider is installed but could not name the current scope. Raised
    rather than guessing — see the module docstring."""


# Provider is process-wide (installed once at plugin load); the override is a
# ContextVar so concurrent requests and background tasks can each run under
# their own scope without leaking into one another. A plain global here would
# be a cross-user data leak under any real concurrency.
_provider: Callable[[], str] | None = None
_override: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "serin_scope_override", default=None
)


def set_scope_provider(provider: Callable[[], str] | None) -> None:
    """Install (or clear) the current-user resolver. Called by a commercial
    pack at plugin-load time; tests clear it with ``None``."""
    global _provider
    _provider = provider


def provider_installed() -> bool:
    """True when this deployment is multi-user."""
    return _provider is not None


def current() -> str:
    """The scope every per-user query is partitioned by.

    An explicit :func:`using` block wins, then the installed provider, then
    the single-user default.
    """
    forced = _override.get()
    if forced is not None:
        return forced
    if _provider is None:
        return LOCAL_SCOPE
    try:
        resolved = _provider()
    except Exception as exc:  # noqa: BLE001 — see module docstring: fail closed
        raise ScopeError(f"scope provider failed: {exc}") from exc
    if not resolved:
        raise ScopeError("scope provider returned no scope")
    return str(resolved)


_lister: Callable[[], list[str]] | None = None


def set_scope_lister(lister: Callable[[], list[str]] | None) -> None:
    """Install (or clear) an enumerator of every scope on this deployment.

    Background work — the briefing scheduler above all — has to act for
    everyone rather than for whoever happens to be making a request. Core has
    no idea what a user is, so a pack that adds accounts supplies this.
    """
    global _lister
    _lister = lister


def all_scopes() -> list[str]:
    """Every scope background work should iterate. One entry on self-host."""
    if _lister is None:
        return [LOCAL_SCOPE]
    try:
        return list(_lister()) or [LOCAL_SCOPE]
    except Exception as exc:  # noqa: BLE001
        raise ScopeError(f"scope lister failed: {exc}") from exc


@contextmanager
def using(scope: str) -> Iterator[str]:
    """Run a block as ``scope``.

    Used by tests, and by background work that has no request to read a user
    from — the scheduler running one user's briefing, say. Restores the
    previous scope even on error.
    """
    token = _override.set(scope)
    try:
        yield scope
    finally:
        _override.reset(token)

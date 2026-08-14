"""Optional app lock — single-user passphrase auth.

Off by default (self-host on localhost). Setting ``SERIN_AUTH_PASSWORD``
turns on the gate: every ``/api/*`` request must present the session token,
either as an ``Authorization: Bearer <token>`` header (mobile / API clients)
or the ``serin_session`` cookie (web UI, set by the login endpoint).

The token is deterministic — ``HMAC(secret_key, "auth:v1:" + password)`` —
so it survives restarts without a session table, and changing either the
password or the secret key revokes every outstanding session. Comparison is
constant-time.

Public even when locked: the SPA shell/assets (the login screen must render),
``/api/auth/*`` and ``/api/v1/version`` (health checks).
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from typing import Any

from backend.config import settings

COOKIE_NAME = "serin_session"

# Paths under /api that stay reachable without a token.
PUBLIC_API_PREFIXES = (
    "/api/auth/",
    "/api/v1/version",
    # Starting a checkout is the whole point of a public pricing page, and the
    # public instance runs locked — without these the "Get Intelligence" button
    # answers "sign in first" to the exact visitor it exists to convert. It
    # creates a Stripe session and reads nothing, so there is no data behind it.
    "/api/billing/checkout",
    "/api/v1/billing/checkout",
    # Cancellation is the same story in reverse: the person it exists for is
    # a subscriber standing on the public site, not someone signed into this
    # instance. It proxies to billing, which proves inbox ownership by email
    # before anything is cancelled — no data lives behind these paths.
    "/api/billing/cancel/",
    "/api/v1/billing/cancel/",
)


# Optional replacement for the whole authorization decision, installed by a
# commercial pack that brings real per-user accounts (see backend/scope.py for
# the matching data seam). Signature: (headers, cookies) -> bool.
_authorizer: Callable[[Any, Any], bool] | None = None


def set_authorizer(authorizer: Callable[[Any, Any], bool] | None) -> None:
    """Install (or clear) a replacement authorization check.

    Installing one also turns the gate *on*: a multi-user deployment has no
    shared passphrase, so ``settings.auth_password`` is empty and the default
    check would wave every request through. An installed authorizer means
    "identity is required", always.
    """
    global _authorizer
    _authorizer = authorizer


# Optional post-authorization veto, also pack-installed. Runs after the
# authorizer has said yes, and may still refuse this particular request —
# e.g. an account whose subscription lapsed keeps read and export access but
# loses actions that cost money. Signature: (method, path) -> None to allow,
# or a user-facing reason; the middleware answers 402 Payment Required with
# it. Distinct from the authorizer because the two answers differ: "who are
# you" is 401 and ends at the login screen, "you may not do this right now"
# is 402 and must leave the session standing.
_request_gate: Callable[[str, str], str | None] | None = None


def set_request_gate(gate: Callable[[str, str], str | None] | None) -> None:
    global _request_gate
    _request_gate = gate


def request_denial(method: str, path: str) -> str | None:
    """The installed gate's objection to this request, or None."""
    if _request_gate is None:
        return None
    return _request_gate(method, path)


def authorizer_installed() -> bool:
    """True when a pack has taken over the authorization decision."""
    return _authorizer is not None


def auth_enabled() -> bool:
    return _authorizer is not None or bool(settings.auth_password.strip())


def session_token() -> str:
    """The one valid bearer/cookie value for the configured password."""
    from backend import secrets_store

    key = secrets_store._load_or_create_key()
    digest = hmac.new(key, f"auth:v1:{settings.auth_password}".encode(), hashlib.sha256)
    return digest.hexdigest()


def verify_password(candidate: str) -> bool:
    return hmac.compare_digest(candidate or "", settings.auth_password)


def verify_token(candidate: str) -> bool:
    return bool(candidate) and hmac.compare_digest(candidate, session_token())


def is_public_path(path: str) -> bool:
    if not path.startswith("/api"):
        return True  # SPA shell + assets — data lives behind /api only
    return any(path.startswith(prefix) or path == prefix.rstrip("/") for prefix in PUBLIC_API_PREFIXES)


def request_is_authorized(headers, cookies) -> bool:
    """Check a request's Authorization header or session cookie."""
    if _authorizer is not None:
        return bool(_authorizer(headers, cookies))
    if not auth_enabled():
        return True
    authorization = headers.get("authorization") or ""
    if authorization.lower().startswith("bearer ") and verify_token(authorization[7:].strip()):
        return True
    return verify_token(cookies.get(COOKIE_NAME, ""))

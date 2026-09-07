"""Scoped, revocable tokens for agent clients (MCP, OpenAPI, scripts).

The app lock in :mod:`backend.auth` issues exactly one token — a deterministic
HMAC of the passphrase — shared by the web UI, the mobile app and every API
client. That is fine for "is this my browser"; it is wrong for an agent. It
carries full read *and write* access to every endpoint, it cannot be named or
listed, and the only way to revoke it is to change the passphrase, which signs
you out everywhere at once.

So agent clients get their own credentials:

- **Named**, so a token in an MCP config is identifiable a year later.
- **Independently revocable**, without disturbing any session.
- **Scoped**: a ``read`` token reaches ``/api/agent/*`` and nothing else. It
  cannot read ``/api/backup``, cannot POST to ``/api/positions``, and cannot
  become a general API key by accident. The agent surface is read-only by
  construction (see :mod:`backend.tools`), so "read" is the only scope that
  exists today; the field is here so adding a second one later is not a
  migration.
- **Stored hashed.** Only ``sha256(token)`` is persisted. A leaked database
  backup does not hand over working credentials, and the plaintext is shown
  exactly once, at issue time.

**The token names its owner, because it has to.** Verification must answer
"whose token is this" before any query runs — a lookup that already knows the
scope is no use for establishing it. Scanning every account's tokens is not an
option either: on Cloud, Postgres row-level security is the real boundary, so
a session bound to one scope simply cannot see another's rows, and a scan
would find nothing rather than fail loudly.

So the owner rides in the token: ``serin_at_<b64url(owner)>.<secret>``. The
owner half is not a secret — it identifies the holder to themselves, and
anyone with the token already has the token — while the secret half is what
verification actually turns on. Swapping the owner segment for someone else's
looks up *their* stored hashes, which will not match, so a token cannot be
retargeted. Tokens issued before this format existed carry no owner segment
and resolve to :data:`~backend.scope.LOCAL_SCOPE`, which is what they always
meant on a single-user box.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import secrets
import time
from datetime import UTC, datetime
from typing import Any

from backend import db

logger = logging.getLogger(__name__)

SETTINGS_KEY = "agent_tokens"

# Recognisable in a config file, greppable in a log, and namespaced so a leaked
# string is obviously ours to revoke rather than an unlabelled secret.
TOKEN_PREFIX = "serin_at_"
_TOKEN_BYTES = 32

READ_SCOPE = "read"
SCOPES = (READ_SCOPE,)

# Every authorized request would otherwise write last_used_at, turning a read
# into a read+write and putting a DB write on the hot path of every tool call.
# Minute granularity is all "when did this token last work" needs.
_TOUCH_INTERVAL_SECONDS = 60


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _load() -> list[dict[str, Any]]:
    raw = db.get_setting(SETTINGS_KEY, "")
    if not raw:
        return []
    try:
        stored = json.loads(raw)
    except json.JSONDecodeError:
        # Corrupt settings row: treat as "no tokens" rather than raising. A
        # bad row must not lock the owner out of the app that would let them
        # fix it — and it fails closed, since no token can then verify.
        return []
    return [row for row in stored if isinstance(row, dict)] if isinstance(stored, list) else []


def _save(rows: list[dict[str, Any]]) -> None:
    db.set_setting(SETTINGS_KEY, json.dumps(rows))


def _public(row: dict[str, Any]) -> dict[str, Any]:
    """A record safe to return over HTTP — everything except the hash."""
    return {k: v for k, v in row.items() if k != "token_sha256"}


def _encode_owner(owner: str) -> str:
    return base64.urlsafe_b64encode(owner.encode("utf-8")).decode("ascii").rstrip("=")


def owner_of(candidate: str) -> str:
    """Which scope a token claims to belong to.

    A *claim*, not a fact — it decides where to look for the hash, and the
    hash decides whether the claim was true. A token with no owner segment
    predates the format and means the single-user scope.
    """
    from backend import scope as scope_module

    if not candidate.startswith(TOKEN_PREFIX):
        return ""
    body = candidate[len(TOKEN_PREFIX):]
    if "." not in body:
        return scope_module.LOCAL_SCOPE
    encoded = body.split(".", 1)[0]
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        return base64.urlsafe_b64decode(padded).decode("utf-8")
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return ""


def issue(name: str, scope: str = READ_SCOPE) -> tuple[dict[str, Any], str]:
    """Create a token for the current scope. Returns ``(record, plaintext)``;
    the plaintext is not recoverable afterwards."""
    from backend import scope as scope_module

    label = (name or "").strip() or "Agent token"
    if scope not in SCOPES:
        raise ValueError(f"Unknown scope {scope!r}; expected one of {', '.join(SCOPES)}")
    owner = scope_module.current()
    plaintext = f"{TOKEN_PREFIX}{_encode_owner(owner)}.{secrets.token_urlsafe(_TOKEN_BYTES)}"
    record = {
        "id": secrets.token_hex(8),
        "name": label[:80],
        "scope": scope,
        "token_sha256": _hash(plaintext),
        "created_at": _now_iso(),
        "last_used_at": None,
    }
    rows = _load()
    rows.append(record)
    _save(rows)
    return _public(record), plaintext


def list_tokens() -> list[dict[str, Any]]:
    """Every live token for this scope, newest first. Never includes hashes."""
    return [_public(row) for row in sorted(_load(), key=lambda r: r.get("created_at") or "", reverse=True)]


def revoke(token_id: str) -> bool:
    """Delete a token. Returns False when the id was already gone, so the
    caller can answer 404 rather than pretending."""
    rows = _load()
    remaining = [row for row in rows if row.get("id") != token_id]
    if len(remaining) == len(rows):
        return False
    _save(remaining)
    return True


def revoke_all() -> int:
    rows = _load()
    _save([])
    return len(rows)


def _touch(token_id: str) -> None:
    """Record that a token was used, at most once a minute (see the constant)."""
    rows = _load()
    now = time.time()
    for row in rows:
        if row.get("id") != token_id:
            continue
        previous = row.get("last_used_at")
        if previous:
            try:
                age = now - datetime.fromisoformat(previous).timestamp()
            except ValueError:
                age = _TOUCH_INTERVAL_SECONDS + 1
            if age < _TOUCH_INTERVAL_SECONDS:
                return
        row["last_used_at"] = _now_iso()
        _save(rows)
        return


def verify(candidate: str) -> dict[str, Any] | None:
    """The record behind a token string, or None. Carries ``owner``.

    The lookup runs inside the owner the token claims, which is what makes it
    work on Cloud at all: the read has to be bound to a scope before row-level
    security will show it anything. A claim to a scope that does not exist, or
    that holds no matching hash, simply finds nothing.

    Compared in constant time against every stored hash, and the loop always
    runs to completion, so a caller cannot learn *which* token matched, or how
    many exist, from the time taken.
    """
    from backend import scope as scope_module

    if not candidate or not candidate.startswith(TOKEN_PREFIX):
        return None
    owner = owner_of(candidate)
    if not owner:
        return None
    digest = _hash(candidate)
    try:
        with scope_module.using(owner):
            matched: dict[str, Any] | None = None
            for row in _load():
                if hmac.compare_digest(str(row.get("token_sha256") or ""), digest):
                    matched = row
            if matched is None:
                return None
            _touch(str(matched.get("id") or ""))
    except Exception:
        # An unreadable scope is an invalid token, not an error to propagate:
        # this runs on the authorization path, where the only useful answer is
        # yes or no.
        logger.debug("Agent token lookup failed for a claimed scope", exc_info=True)
        return None
    return {**_public(matched), "owner": owner}


def scope_allows(record: dict[str, Any], method: str, path: str) -> bool:
    """Whether a token may make this request.

    Deliberately an allowlist of one prefix rather than "reads are fine": a
    GET is not automatically harmless (``/api/backup`` streams the whole
    database), and the point of an agent credential is that it reaches the
    agent surface only.
    """
    if record.get("scope") != READ_SCOPE:
        return False
    if not (path == "/api/agent" or path.startswith("/api/agent/")):
        return False
    # The tool surface is read-only by construction; POST is how a call
    # carries its arguments, not a mutation.
    return method.upper() in {"GET", "HEAD", "POST", "OPTIONS"}

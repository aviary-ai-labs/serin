"""Connector registry — discovery, config, and enable-state.

In-tree connectors self-register with ``@register`` when their module is
imported (see ``backend/connectors/__init__.py``, which imports them all).
Per-connector config and enable-state live in the SQLite ``app_settings``
table, so they survive restarts and are editable from the portal.

Config resolution is **DB-first with a settings/env fallback**: a value set
in the portal wins, otherwise the connector falls back to the matching
environment variable. That keeps a fresh ``.env``-only install working while
making the portal the primary way to configure connectors.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from backend import db, scope
from backend.connectors.base import Connector, ConnectorManifest, TestResult

logger = logging.getLogger(__name__)

_REGISTRY: dict[str, type[Connector]] = {}

# A secret arriving blank means "unchanged" — the portal renders a mask, and a
# blank save must not wipe the key behind it. Deleting one therefore needs a
# value no real secret can be.
CLEAR_SECRET = "__serin_clear__"

# Callbacks run after a connector's config is saved. The pack uses this to
# re-decide managed AI: whether it engages depends on whether the user has a
# key of their own, which is exactly what a save can change.
_CONFIG_LISTENERS: list[Callable[[str], None]] = []


def on_config_saved(callback: Callable[[str], None]) -> None:
    """Register a callback invoked with the connector id after each save."""
    _CONFIG_LISTENERS.append(callback)


def _config_saved(connector_id: str) -> None:
    for callback in list(_CONFIG_LISTENERS):
        try:
            callback(connector_id)
        except Exception:  # a listener must never fail the save it observes
            logger.exception("connector config listener failed for %s", connector_id)


def register(cls: type[Connector]) -> type[Connector]:
    manifest = getattr(cls, "manifest", None)
    if manifest is None:
        raise ValueError(f"{cls.__name__} has no manifest")
    _REGISTRY[manifest.id] = cls
    return cls


def get_class(connector_id: str) -> type[Connector] | None:
    return _REGISTRY.get(connector_id)


def has(connector_id: str) -> bool:
    return connector_id in _REGISTRY


def all_manifests() -> list[ConnectorManifest]:
    return [cls.manifest for cls in _REGISTRY.values()]


def manifests_by_kind(kind: str) -> list[ConnectorManifest]:
    return [m for m in all_manifests() if m.kind == kind]


# --- config + enable storage ------------------------------------------------


def _instance_get(key: str, default: str = "") -> str:
    """Connector config belongs to the deployment, not to whoever is logged in:
    in Cloud one operator key serves every user, and startup tasks that touch
    it have no user at all."""
    with scope.using(scope.INSTANCE_SCOPE):
        return db.get_setting(key, default)


def _instance_set(key: str, value: str) -> None:
    with scope.using(scope.INSTANCE_SCOPE):
        db.set_setting(key, value)


def _config_key(connector_id: str) -> str:
    return f"connector:{connector_id}:config"


def _enabled_key(connector_id: str) -> str:
    return f"connector:{connector_id}:enabled"


def _secret_keys(connector_id: str) -> set[str]:
    cls = get_class(connector_id)
    if cls is None:
        return set()
    return {f.key for f in cls.manifest.config_schema if f.secret}


def _user_keys(connector_id: str) -> set[str]:
    """Fields belonging to the signed-in person rather than the deployment."""
    cls = get_class(connector_id)
    if cls is None:
        return set()
    return {f.key for f in cls.manifest.config_schema if f.owner == "user"}


def _user_get(key: str, default: str = "") -> str:
    """The current scope's own value. On self-host that is the single local
    scope, so this and ``_instance_get`` differ in name only."""
    return db.get_setting(key, default)


def _user_set(key: str, value: str) -> None:
    db.set_setting(key, value)


def instance_config_is_writable() -> bool:
    """Whether deployment-level connector config may be edited through the API.

    On a single-user install, yes — the person at the keyboard *is* the
    operator, and the portal is how they configure things.

    On a shared deployment, no. Every account is an ordinary customer; there
    is no operator among them, and one of them repointing ``fmp.base_url`` at
    a host they control would serve fabricated prices to everybody. So these
    values come from the environment, where only whoever runs the deployment
    can set them.

    Keyed off the scope provider rather than an env var of its own, so it
    cannot drift out of step with whether accounts are actually switched on.
    """
    return not scope.provider_installed()


def _decode(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def get_config(connector_id: str) -> dict[str, Any]:
    """The effective config: the deployment's fields, with the caller's own
    layered over them.

    Reading falls back to the instance blob for user-owned fields that have no
    per-user value yet. That is what makes this migration-free — an existing
    install keeps answering with what it has always answered with, and the
    value only moves the first time somebody saves it.
    """
    value = _decode(_instance_get(_config_key(connector_id)))
    user_keys = _user_keys(connector_id)
    if user_keys:
        mine = _decode(_user_get(_config_key(connector_id)))
        for key in user_keys:
            if key in mine:
                value[key] = mine[key]
            # else: leave the instance value in place as the legacy fallback

    # Secrets are encrypted at rest; decrypt on the way out (legacy plaintext
    # values pass through until encrypt_existing_secrets migrates them).
    from backend import secrets_store

    for key in _secret_keys(connector_id):
        stored = value.get(key)
        if isinstance(stored, str) and stored:
            value[key] = secrets_store.decrypt(stored)
    return value


def set_config(connector_id: str, config: dict[str, Any]) -> dict[str, Any]:
    """Merge incoming config over the stored config, filing each field by owner.

    Secret fields that arrive blank are treated as "unchanged" so the portal
    can render a masked field without wiping the stored secret on save; a
    field set to ``CLEAR_SECRET`` is deleted instead, which is the only way to
    take a key back out once it is in.
    Secret values are AES-GCM encrypted before they touch the database.

    Instance-owned fields are refused outright on a shared deployment — see
    ``instance_config_is_writable``. They are the deployment's, and there is no
    operator role among the accounts to distinguish who may set them.
    """
    from backend import secrets_store

    secret_keys = _secret_keys(connector_id)
    user_keys = _user_keys(connector_id)

    current = get_config(connector_id)  # decrypted, merged view
    merged = dict(current)
    writable_instance = instance_config_is_writable()
    ignored: list[str] = []
    cleared: list[str] = []
    for key, value in (config or {}).items():
        if key in secret_keys and (value is None or value == ""):
            continue  # keep existing secret
        if key in secret_keys and value == CLEAR_SECRET:
            if key not in user_keys and not writable_instance:
                ignored.append(key)
                continue
            merged.pop(key, None)
            cleared.append(key)
            continue
        if key not in user_keys and not writable_instance:
            # Operator-owned; set it in the environment, not here. Recorded
            # rather than merely dropped: a client that round-trips the whole
            # config would otherwise get a 200 and no hint that half of what it
            # sent went nowhere. Refusing the whole request instead would break
            # that round-trip for anyone who only meant to change a preference.
            ignored.append(key)
            continue
        merged[key] = value

    def _encrypted(subset: dict[str, Any]) -> dict[str, Any]:
        out = dict(subset)
        for key in secret_keys:
            if isinstance(out.get(key), str) and out[key]:
                out[key] = secrets_store.encrypt(out[key])
        return out

    if user_keys:
        _user_set(
            _config_key(connector_id),
            json.dumps(_encrypted({k: v for k, v in merged.items() if k in user_keys})),
        )
    instance_part = {k: v for k, v in merged.items() if k not in user_keys}
    # `cleared` keeps the write happening when a removal empties the instance
    # part — skipping it there would leave the deleted key sitting in the row.
    if writable_instance and (instance_part or cleared):
        _instance_set(_config_key(connector_id), json.dumps(_encrypted(instance_part)))
    _config_saved(connector_id)
    # Reported alongside the result rather than stored, so nothing about a
    # refused write ends up persisted in the config it was refused from.
    return dict(merged, ignored_fields=ignored) if ignored else merged


def encrypt_existing_secrets() -> int:
    """Startup migration: encrypt any legacy plaintext secrets in place.

    Returns the number of values migrated. Idempotent — already-encrypted
    values are left untouched.
    """
    from backend import secrets_store

    migrated = 0
    for manifest in all_manifests():
        secret_keys = _secret_keys(manifest.id)
        if not secret_keys:
            continue
        raw = _instance_get(_config_key(manifest.id))
        if not raw:
            continue
        try:
            stored = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(stored, dict):
            continue
        changed = False
        for key in secret_keys:
            value = stored.get(key)
            if isinstance(value, str) and value and not secrets_store.is_encrypted(value):
                stored[key] = secrets_store.encrypt(value)
                changed = True
                migrated += 1
        if changed:
            _instance_set(_config_key(manifest.id), json.dumps(stored))
    return migrated


def has_setting(connector_id: str) -> bool:
    """True if the user has touched this connector in the portal (config or
    enable state stored). Used to decide whether portal config overrides the
    settings/env defaults."""
    return bool(
        _instance_get(_config_key(connector_id))
        or _instance_get(_enabled_key(connector_id))
        or (
            _personal(connector_id)
            and (_user_get(_config_key(connector_id)) or _user_get(_enabled_key(connector_id)))
        )
    )


def _personal(connector_id: str) -> bool:
    """Whether this connector is configured per person rather than per
    deployment. Owning any user-level field is what makes it personal: if your
    exchange key is yours, then so is whether it is switched on."""
    return bool(_user_keys(connector_id))


def is_enabled(connector_id: str) -> bool:
    if _personal(connector_id):
        raw = _user_get(_enabled_key(connector_id))
        if raw not in ("1", "0"):
            # Legacy single-user installs stored this instance-wide; keep
            # honouring that until the switch is next touched.
            raw = _instance_get(_enabled_key(connector_id))
    else:
        raw = _instance_get(_enabled_key(connector_id))
    if raw in ("1", "0"):
        return raw == "1"
    cls = get_class(connector_id)
    return cls.manifest.default_enabled if cls is not None else False


def set_enabled(connector_id: str, enabled: bool) -> bool:
    value = "1" if enabled else "0"
    if _personal(connector_id):
        _user_set(_enabled_key(connector_id), value)
    elif instance_config_is_writable():
        _instance_set(_enabled_key(connector_id), value)
    else:
        # Market data and managed AI are the deployment's to run. One customer
        # must not be able to switch off everyone's price feed.
        raise PermissionError(
            f"{connector_id} is managed by this deployment and cannot be changed here."
        )
    return enabled


def instantiate(connector_id: str) -> Connector | None:
    cls = get_class(connector_id)
    if cls is None:
        return None
    return cls(get_config(connector_id))


def test(connector_id: str) -> TestResult:
    connector = instantiate(connector_id)
    if connector is None:
        return TestResult(ok=False, message=f"Unknown connector: {connector_id}")
    try:
        return connector.test()
    except Exception as exc:  # a connector's test must never crash the portal
        return TestResult(ok=False, message=f"{type(exc).__name__}: {exc}")


def public_config(connector_id: str) -> dict[str, Any]:
    """Config for the API — secret values masked to a boolean 'is set' flag."""
    cls = get_class(connector_id)
    stored = get_config(connector_id)
    if cls is None:
        return stored
    out: dict[str, Any] = {}
    for field_def in cls.manifest.config_schema:
        value = stored.get(field_def.key, field_def.default)
        if field_def.secret:
            out[field_def.key] = ""
            out[f"{field_def.key}__is_set"] = bool(stored.get(field_def.key))
        else:
            out[field_def.key] = value
    return out

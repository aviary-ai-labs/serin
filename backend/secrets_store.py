"""Encryption-at-rest for secret values stored in SQLite.

Connector config fields marked ``secret=True`` (API keys, consumer keys) are
envelope-encrypted with AES-256-GCM before they touch ``app_settings``. The
data key comes from, in order:

1. ``FINCH_SECRET_KEY`` env var — base64 or hex, 32 bytes. Set this in
   deployments so the key never lives next to the database.
2. A key file next to the database (``.finch-key``), auto-generated on first
   use with 0600 permissions. Zero-setup default for self-hosters.

Ciphertext format: ``enc:v1:<base64(nonce || ciphertext+tag)>``. Plaintext
values (pre-encryption rows) still decrypt as themselves, and
``encrypt_existing_secrets()`` migrates them in place on startup.

Threat model: protects secrets when the DB file alone leaks (backups, copied
volumes). An attacker with the key file or a live process can still read them
— full-disk or OS-keychain protection stays on the roadmap.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import secrets as pysecrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from backend import db

logger = logging.getLogger(__name__)

PREFIX = "enc:v1:"
KEY_FILE_NAME = ".serin-key"
# Pre-rename key file — still read (never written) so secrets encrypted before
# the Finch→Serin rename keep decrypting.
LEGACY_KEY_FILE_NAME = ".finch-key"
_key_cache: bytes | None = None


def _decode_key_material(raw: str) -> bytes | None:
    raw = raw.strip()
    for decoder in (base64.b64decode, bytes.fromhex):
        try:
            key = decoder(raw)
            if len(key) == 32:
                return key
        except (ValueError, binascii.Error):
            continue
    return None


def _key_file_path() -> Path:
    # Follows db.set_db_path (tests, custom deployments) — the key lives next
    # to whichever database is actually in use.
    return db.DB_PATH.parent / KEY_FILE_NAME


def _load_or_create_key() -> bytes:
    global _key_cache
    if _key_cache is not None:
        return _key_cache

    env_material = os.environ.get("SERIN_SECRET_KEY") or os.environ.get("FINCH_SECRET_KEY", "")
    if env_material:
        key = _decode_key_material(env_material)
        if key is None:
            raise RuntimeError(
                "SERIN_SECRET_KEY (or legacy FINCH_SECRET_KEY) is set but is not "
                "32 bytes of base64 or hex."
            )
        _key_cache = key
        return key

    path = _key_file_path()
    if not path.exists():
        # Fall back to a pre-rename key so existing encrypted secrets decrypt.
        legacy = path.parent / LEGACY_KEY_FILE_NAME
        if legacy.exists():
            path = legacy
    if path.exists():
        key = _decode_key_material(path.read_text(encoding="utf-8"))
        if key is None:
            raise RuntimeError(f"Key file {path} is corrupt — expected 32 bytes of base64.")
        _key_cache = key
        return key

    path.parent.mkdir(parents=True, exist_ok=True)
    key = pysecrets.token_bytes(32)
    path.write_text(base64.b64encode(key).decode("ascii"), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover — e.g. exotic filesystems
        logger.warning("Could not chmod %s to 0600", path)
    logger.info("Generated new secrets key file at %s", path)
    _key_cache = key
    return key


def reset_key_cache() -> None:
    """Test hook: forget the cached key (e.g. after switching db_path)."""
    global _key_cache
    _key_cache = None


def is_encrypted(value: str) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt(value: str) -> str:
    """Encrypt a secret string. Empty values and already-encrypted values pass
    through unchanged (idempotent)."""
    if not value or is_encrypted(value):
        return value
    key = _load_or_create_key()
    nonce = pysecrets.token_bytes(12)
    ciphertext = AESGCM(key).encrypt(nonce, value.encode("utf-8"), None)
    return PREFIX + base64.b64encode(nonce + ciphertext).decode("ascii")


def decrypt(value: str) -> str:
    """Decrypt a stored secret. Plaintext (legacy) values return as-is; an
    unreadable ciphertext (wrong/lost key) returns "" with a logged error
    rather than crashing every request."""
    if not value or not is_encrypted(value):
        return value
    try:
        blob = base64.b64decode(value[len(PREFIX):])
        nonce, ciphertext = blob[:12], blob[12:]
        key = _load_or_create_key()
        return AESGCM(key).decrypt(nonce, ciphertext, None).decode("utf-8")
    except Exception:
        logger.error(
            "Could not decrypt a stored secret — the key file may have been "
            "lost or FINCH_SECRET_KEY changed. Re-enter the secret in the "
            "connector portal."
        )
        return ""

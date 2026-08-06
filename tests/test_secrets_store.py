"""Secrets encryption at rest: roundtrip, storage format, legacy migration."""

from __future__ import annotations

import json
import os
import stat

import pytest
from backend import db, scope, secrets_store
from backend.connectors import registry


@pytest.fixture
def fresh(tmp_path):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()
    secrets_store.reset_key_cache()
    yield tmp_path
    secrets_store.reset_key_cache()


def test_encrypt_roundtrip_and_format(fresh):
    ciphertext = secrets_store.encrypt("sk-super-secret")
    assert ciphertext.startswith("enc:v1:")
    assert "sk-super-secret" not in ciphertext
    assert secrets_store.decrypt(ciphertext) == "sk-super-secret"
    # Idempotent: encrypting ciphertext is a no-op; empty passes through.
    assert secrets_store.encrypt(ciphertext) == ciphertext
    assert secrets_store.encrypt("") == ""
    assert secrets_store.decrypt("plaintext-legacy") == "plaintext-legacy"


def test_key_file_created_with_0600(fresh):
    secrets_store.encrypt("anything")
    key_file = fresh / secrets_store.KEY_FILE_NAME
    assert key_file.exists()
    mode = stat.S_IMODE(os.stat(key_file).st_mode)
    assert mode == 0o600


def test_env_key_overrides_key_file(fresh, monkeypatch):
    import base64

    key = base64.b64encode(bytes(range(32))).decode()
    monkeypatch.setenv("SERIN_SECRET_KEY", key)
    secrets_store.reset_key_cache()

    ciphertext = secrets_store.encrypt("with-env-key")
    assert secrets_store.decrypt(ciphertext) == "with-env-key"
    # No key file needed when the env key is authoritative.
    assert not (fresh / secrets_store.KEY_FILE_NAME).exists()


def test_registry_stores_ciphertext_but_returns_plaintext(fresh):
    registry.set_config("ai_briefing", {"deepseek_api_key": "sk-ds-123", "provider": "deepseek"})

    with scope.using(scope.INSTANCE_SCOPE):
        raw = db.get_setting("connector:ai_briefing:config")
    stored = json.loads(raw)
    assert stored["deepseek_api_key"].startswith("enc:v1:")
    assert "sk-ds-123" not in raw
    # Non-secret fields stay readable in place.
    assert stored["provider"] == "deepseek"

    assert registry.get_config("ai_briefing")["deepseek_api_key"] == "sk-ds-123"


def test_blank_secret_on_save_keeps_existing(fresh):
    registry.set_config("ai_briefing", {"deepseek_api_key": "sk-keep-me"})
    registry.set_config("ai_briefing", {"deepseek_api_key": "", "style": "analyst"})
    config = registry.get_config("ai_briefing")
    assert config["deepseek_api_key"] == "sk-keep-me"
    assert config["style"] == "analyst"


def test_migration_encrypts_legacy_plaintext(fresh):
    # Simulate a pre-encryption row written directly as plaintext JSON.
    with scope.using(scope.INSTANCE_SCOPE):
        db.set_setting("connector:ai_briefing:config", json.dumps({"anthropic_api_key": "sk-legacy"}))

    migrated = registry.encrypt_existing_secrets()

    assert migrated == 1
    with scope.using(scope.INSTANCE_SCOPE):
        raw = db.get_setting("connector:ai_briefing:config")
    assert "sk-legacy" not in raw
    assert registry.get_config("ai_briefing")["anthropic_api_key"] == "sk-legacy"
    # Second run is a no-op.
    assert registry.encrypt_existing_secrets() == 0

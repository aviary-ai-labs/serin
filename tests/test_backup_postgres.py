"""Encrypted Postgres backups — the crypto and the guardrails.

One database now holds every Cloud customer's portfolio, so a backup that
silently isn't encrypted, or that can't actually be restored, is the kind of
thing you find out about at the worst moment.
"""

from __future__ import annotations

import pytest
from cryptography.exceptions import InvalidTag
from scripts import backup_postgres as bp


def test_roundtrip(monkeypatch):
    monkeypatch.setenv("BACKUP_MASTER_KEY", "a-master-key")
    blob = b"PGDMP fake dump bytes \x00\x01\x02"
    assert bp.decrypt(bp.encrypt(blob)) == blob


def test_ciphertext_does_not_contain_the_plaintext(monkeypatch):
    monkeypatch.setenv("BACKUP_MASTER_KEY", "a-master-key")
    secret = b"alice@example.com holds 1000 AAPL"
    assert secret not in bp.encrypt(secret)


def test_a_different_master_key_cannot_read_it(monkeypatch):
    monkeypatch.setenv("BACKUP_MASTER_KEY", "the-real-key")
    sealed = bp.encrypt(b"every customer's portfolio")
    monkeypatch.setenv("BACKUP_MASTER_KEY", "some-other-key")
    with pytest.raises(InvalidTag):
        bp.decrypt(sealed)


def test_tampering_is_detected(monkeypatch):
    """AES-GCM authenticates, so a truncated or edited backup fails loudly
    instead of restoring corrupt data."""
    monkeypatch.setenv("BACKUP_MASTER_KEY", "a-master-key")
    sealed = bytearray(bp.encrypt(b"important bytes"))
    sealed[-1] ^= 0x01
    with pytest.raises(InvalidTag):
        bp.decrypt(bytes(sealed))
    with pytest.raises(InvalidTag):
        bp.decrypt(bytes(sealed[:-5]))


def test_refuses_to_run_without_a_master_key(monkeypatch):
    """Better to fail than to write every customer's data out in the clear."""
    monkeypatch.delenv("BACKUP_MASTER_KEY", raising=False)
    with pytest.raises(SystemExit):
        bp.encrypt(b"anything")


def test_refuses_a_non_postgres_url(monkeypatch):
    monkeypatch.setenv("BACKUP_MASTER_KEY", "a-master-key")
    monkeypatch.delenv("SERIN_DATABASE_URL", raising=False)
    with pytest.raises(SystemExit):
        bp._database_url()

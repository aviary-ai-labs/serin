"""Tests for the connector platform — registry, config, enable-state, the
API, and market-data resolution.
"""

from __future__ import annotations

from backend import connectors, db, main
from backend.config import settings
from backend.connectors import registry
from fastapi.testclient import TestClient


def _fresh_db(tmp_path):
    db.set_db_path(tmp_path / "connectors-test.db")
    db.init_db()


# --- registry / discovery ---------------------------------------------------

def test_all_expected_connectors_registered():
    ids = {m.id for m in registry.all_manifests()}
    assert {"yahoo", "fmp", "snaptrade", "generic_csv", "ai_briefing"} <= ids


def test_manifests_grouped_by_kind():
    kinds = {m.id: m.kind for m in registry.all_manifests()}
    assert kinds["yahoo"] == "market_data"
    assert kinds["snaptrade"] == "holdings"
    assert kinds["ai_briefing"] == "insight"


# --- config storage ---------------------------------------------------------

def test_config_roundtrip(tmp_path):
    _fresh_db(tmp_path)
    registry.set_config("fmp", {"api_key": "sk-secret", "base_url": "https://example.test"})
    cfg = registry.get_config("fmp")
    assert cfg["api_key"] == "sk-secret"
    assert cfg["base_url"] == "https://example.test"


def test_public_config_masks_secrets(tmp_path):
    _fresh_db(tmp_path)
    registry.set_config("fmp", {"api_key": "sk-secret"})
    public = registry.public_config("fmp")
    assert public["api_key"] == ""           # never returned in plaintext
    assert public["api_key__is_set"] is True


def test_blank_secret_does_not_wipe_stored_secret(tmp_path):
    _fresh_db(tmp_path)
    registry.set_config("fmp", {"api_key": "sk-secret"})
    # A subsequent save with a blank secret (portal rendered masked) keeps it.
    registry.set_config("fmp", {"api_key": "", "base_url": "https://x.test"})
    cfg = registry.get_config("fmp")
    assert cfg["api_key"] == "sk-secret"
    assert cfg["base_url"] == "https://x.test"


# --- enable state -----------------------------------------------------------

def test_default_enabled_state(tmp_path):
    _fresh_db(tmp_path)
    assert registry.is_enabled("yahoo") is True       # market data on by default
    assert registry.is_enabled("fmp") is False        # opt-in (needs a key)
    assert registry.is_enabled("ai_briefing") is False  # insight, opt-in


def test_enable_toggle_persists(tmp_path):
    _fresh_db(tmp_path)
    registry.set_enabled("ai_briefing", True)
    assert registry.is_enabled("ai_briefing") is True
    assert registry.has_setting("ai_briefing") is True


def test_generic_csv_test_requires_mapping(tmp_path):
    _fresh_db(tmp_path)
    # Defaults include symbol/quantity, so test() should pass out of the box.
    result = registry.test("generic_csv")
    assert result.ok is True


# --- market-data resolution -------------------------------------------------

def test_active_market_data_falls_back_to_settings(tmp_path, monkeypatch):
    _fresh_db(tmp_path)
    monkeypatch.setattr(settings, "market_data_provider", "auto")
    monkeypatch.setattr(settings, "fmp_api_key", "")
    # No portal config touched → settings resolution → yahoo (free fallback).
    assert connectors.active_market_data_id() == "yahoo"


def test_portal_enable_overrides_settings(tmp_path, monkeypatch):
    _fresh_db(tmp_path)
    monkeypatch.setattr(settings, "market_data_provider", "auto")
    monkeypatch.setattr(settings, "fmp_api_key", "")
    # User enables FMP in the portal with a key → FMP wins.
    registry.set_enabled("fmp", True)
    registry.set_config("fmp", {"api_key": "sk-portal"})
    assert connectors.active_market_data_id() == "fmp"


# --- API --------------------------------------------------------------------

def test_connector_api_catalog_and_config(tmp_path):
    _fresh_db(tmp_path)
    client = TestClient(main.app)

    catalog = client.get("/api/connectors")
    assert catalog.status_code == 200
    ids = {c["manifest"]["id"] for c in catalog.json()["connectors"]}
    assert "yahoo" in ids and "ai_briefing" in ids

    # Save an FMP key; the response masks it.
    saved = client.put("/api/connectors/fmp/config", json={"config": {"api_key": "sk-test"}})
    assert saved.status_code == 200
    assert saved.json()["config"]["api_key"] == ""
    assert saved.json()["config"]["api_key__is_set"] is True

    # Enable the briefing connector.
    enabled = client.post("/api/connectors/ai_briefing/enable", json={"enabled": True})
    assert enabled.status_code == 200
    assert enabled.json()["enabled"] is True

    # Unknown connector → 404.
    assert client.get("/api/connectors/nope").status_code == 404


def test_connector_api_v1_alias(tmp_path):
    _fresh_db(tmp_path)
    client = TestClient(main.app)
    assert client.get("/api/v1/connectors").status_code == 200

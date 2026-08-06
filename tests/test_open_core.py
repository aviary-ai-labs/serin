"""Open-core seams: external plugin loading + entitlements."""

from __future__ import annotations

import pytest
from backend import db, entitlements
from backend.connectors import registry
from backend.main import app
from backend.plugins import load_external_plugins
from fastapi.testclient import TestClient

GOOD_PLUGIN = '''
from backend.connectors import ConnectorManifest, MarketDataConnector
from backend.connectors.registry import register


@register
class PluginConnector(MarketDataConnector):
    manifest = ConnectorManifest(
        id="testplug",
        name="Test Plugin Source",
        kind="market_data",
        description="Loaded from SERIN_PLUGINS_DIR in a test.",
        default_enabled=False,
    )

    def refresh_prices(self, positions):
        return {"prices": {}, "errors": []}

    def fetch_history(self, period, symbols, positions_by_symbol):
        return {"history": {}, "errors": []}

    def quote(self, symbol, asset_type):
        return None
'''

ENTITLED_PLUGIN = '''
from backend import entitlements

entitlements.set_verifier(lambda: {"plan": "intelligence", "features": ["xray", "managed_ai"]})
'''


@pytest.fixture(autouse=True)
def clean_seams():
    yield
    entitlements.set_verifier(None)
    registry._REGISTRY.pop("testplug", None)


@pytest.fixture(autouse=True)
def isolate_installed_pack(monkeypatch, tmp_path):
    """The loader always scans the app-installed pack dir (<data>/plugins) as a
    fallback, so on a dev box with the real pack installed these tests would
    load serin_pro and fail. Point the fallback somewhere empty."""
    monkeypatch.setattr(
        "backend.plugins.installed_pack_dir", lambda: tmp_path / "no-installed-pack"
    )


def test_plugin_dir_loads_connectors_via_public_sdk(tmp_path):
    (tmp_path / "my_source.py").write_text(GOOD_PLUGIN)

    result = load_external_plugins(tmp_path)

    assert result["loaded"] == ["my_source"]
    assert result["errors"] == {}
    assert registry.has("testplug")
    manifest = registry.get_class("testplug").manifest
    assert manifest.name == "Test Plugin Source"


def test_broken_plugin_is_skipped_not_fatal(tmp_path):
    (tmp_path / "bad.py").write_text("raise RuntimeError('boom at import')\n")
    (tmp_path / "good.py").write_text(GOOD_PLUGIN)

    result = load_external_plugins(tmp_path)

    assert "bad" in result["errors"]
    assert "RuntimeError" in result["errors"]["bad"]
    assert "good" in result["loaded"]
    assert registry.has("testplug")


def test_no_plugins_dir_is_a_noop():
    assert load_external_plugins("") == {"loaded": [], "errors": {}}
    assert load_external_plugins("/nonexistent/path/for/test") == {"loaded": [], "errors": {}}


def test_underscore_and_hidden_files_are_ignored(tmp_path):
    (tmp_path / "_private.py").write_text("raise RuntimeError('must not import')\n")
    (tmp_path / ".hidden.py").write_text("raise RuntimeError('must not import')\n")
    result = load_external_plugins(tmp_path)
    assert result == {"loaded": [], "errors": {}}


def test_entitlements_default_to_open_source(tmp_path):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()
    assert entitlements.summary() == {"plan": "opensource", "features": []}
    assert entitlements.has("xray") is False

    client = TestClient(app)
    payload = client.get("/api/entitlements").json()
    assert payload["plan"] == "opensource"
    assert payload["features"] == []


def test_pack_plugin_installs_verifier(tmp_path):
    (tmp_path / "pro_pack.py").write_text(ENTITLED_PLUGIN)

    load_external_plugins(tmp_path)

    assert entitlements.has("xray") is True
    assert entitlements.summary()["plan"] == "intelligence"


def test_crashing_verifier_fails_open_source():
    def broken():
        raise RuntimeError("license server exploded")

    entitlements.set_verifier(broken)
    assert entitlements.summary() == {"plan": "opensource", "features": []}
    assert entitlements.has("anything") is False

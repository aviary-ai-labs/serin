"""Connector trust posture — classification + API payload exposure."""

from __future__ import annotations

from backend.connectors import registry
from backend.connectors.base import ConnectorManifest


def test_holdings_posture_classification():
    posture = {m.id: m.connect_method for m in registry.manifests_by_kind("holdings")}
    assert posture["snaptrade"] == "oauth"    # Connect — broker login
    assert posture["generic_csv"] == "file"   # Import — a statement, no access
    assert posture["coinbase"] == "api_key"   # Advanced — self-host only
    assert posture["binance"] == "api_key"    # Advanced — self-host only


def test_manifest_to_dict_exposes_connect_method():
    manifest = next(m for m in registry.all_manifests() if m.id == "snaptrade")
    assert manifest.to_dict()["connect_method"] == "oauth"


def test_default_connect_method_is_api_key():
    manifest = ConnectorManifest(id="x", name="X", kind="holdings", description="")
    assert manifest.connect_method == "api_key"

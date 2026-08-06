"""Generic insight-run endpoint — the surface a plugin pack (X-ray) uses."""

from __future__ import annotations

import pytest
from backend import db
from backend.connectors import registry
from backend.connectors.base import ConnectorManifest, InsightConnector
from fastapi.testclient import TestClient

from backend.main import app  # isort: skip  (after registry import)


class _DummyInsight(InsightConnector):
    manifest = ConnectorManifest(id="_dummy_insight", name="Dummy", kind="insight", description="")

    def run(self, context=None):
        return {"ok": True, "echo": context}


@pytest.fixture
def client(tmp_path):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()
    with TestClient(app) as c:
        yield c


def test_run_unknown_connector_is_404(client):
    assert client.post("/api/connectors/nope/run").status_code == 404


def test_run_non_insight_is_400(client):
    # Yahoo is a market-data connector with no run() → not runnable.
    assert client.post("/api/connectors/yahoo/run").status_code == 400


def test_run_insight_executes(client):
    registry.register(_DummyInsight)
    try:
        resp = client.post("/api/connectors/_dummy_insight/run", json={"context": {"x": 1}})
        assert resp.status_code == 200
        assert resp.json()["echo"] == {"x": 1}
    finally:
        registry._REGISTRY.pop("_dummy_insight", None)

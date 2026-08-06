"""Self-host → Serin Cloud migration — export bundle → tenant /api/restore."""

from __future__ import annotations

import pytest
from backend import db
from backend.main import app
from backend.models import PositionIn
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path):
    db.set_db_path(tmp_path / "serin.db")
    db.init_db()
    db.create_position(PositionIn(symbol="AAPL", broker="manual", asset_type="stock",
                                  quantity=10, average_cost=100, current_price=200))
    with TestClient(app) as c:
        yield c


def test_migrate_requires_consent(client):
    r = client.post("/api/cloud/migrate", json={"target_url": "https://x.serin.money"})
    assert r.status_code == 400  # confirm defaults to False


def test_migrate_requires_https(client):
    r = client.post("/api/cloud/migrate", json={"target_url": "http://x", "confirm": True})
    assert r.status_code == 400


def test_migrate_posts_bundle_to_target(client, monkeypatch):
    captured = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"ok": True, "restored": {"positions": 1}}

    class FakeAsyncClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, files=None, headers=None):
            captured["url"] = url
            captured["auth"] = (headers or {}).get("authorization")
            captured["body"] = files["file"][1]
            return FakeResp()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    r = client.post(
        "/api/cloud/migrate",
        json={"target_url": "https://buyer.serin.money/", "token": "one-time-abc", "confirm": True},
    )
    assert r.status_code == 200
    assert r.json()["restored"] == {"positions": 1}
    assert captured["url"] == "https://buyer.serin.money/api/restore"
    assert captured["auth"] == "Bearer one-time-abc"
    # The bundle carries positions but NOT connector secrets (export_data omits them).
    body = captured["body"].decode()
    assert "AAPL" in body
    assert "consumer_key" not in body and "api_key" not in body

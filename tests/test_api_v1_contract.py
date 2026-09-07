"""The versioned contract has a write half.

Every route below existed under /api/ and was simply never aliased, which made
/api/v1 quietly read-only. The mobile client is the only consumer of the
versioned paths, so nothing on the web noticed: adding, editing and deleting a
holding all answered 405, and pull-to-refresh's re-quote is fire-and-forget so
its 405 was swallowed and the gesture just never fetched a price.
"""

from __future__ import annotations

import pytest
from backend import db
from backend.config import settings
from backend.main import app
from fastapi.testclient import TestClient

BODY = {
    "symbol": "AAPL", "name": "Apple", "broker": "Fidelity",
    "asset_type": "stock", "quantity": 10, "average_cost": 100.0,
    "current_price": 150.0, "currency": "USD",
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    db.set_db_path(tmp_path / "v1-contract.db")
    db.init_db()
    monkeypatch.setattr(settings, "market_data_provider", "none", raising=False)
    return TestClient(app)


def test_v1_can_create_update_and_delete_a_position(client):
    created = client.post("/api/v1/positions", json=BODY)
    assert created.status_code == 200, created.text
    position_id = created.json()["id"]

    edited = client.put(f"/api/v1/positions/{position_id}", json={**BODY, "quantity": 25})
    assert edited.status_code == 200, edited.text
    assert edited.json()["quantity"] == 25

    assert client.delete(f"/api/v1/positions/{position_id}").status_code == 200
    assert all(p["id"] != position_id for p in client.get("/api/v1/positions").json())


def test_v1_price_refresh_is_reachable(client):
    """Pull to refresh calls this and ignores the result, so a 405 here is
    invisible in the app — the gesture animates and does nothing."""
    assert client.post("/api/v1/prices/refresh", json={}).status_code == 200


def test_v1_and_unversioned_positions_are_the_same_resource(client):
    """The alias must not become a second, diverging endpoint."""
    created = client.post("/api/v1/positions", json=BODY).json()
    assert any(p["id"] == created["id"] for p in client.get("/api/positions").json())

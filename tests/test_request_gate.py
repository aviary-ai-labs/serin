"""The post-authorization veto seam (auth.set_request_gate).

The authorizer answers "who are you" — a no is 401 and ends at the login
screen. The gate answers "you may not do this right now" — a no is 402 and
must leave the session standing. A pack uses it to keep a lapsed
subscription's account able to browse and export while refusing whatever
costs money. Core's whole job here: consult the gate only after
authorization passes, put its reason on the wire verbatim, and stay out of
the way entirely when nothing is installed.
"""

from __future__ import annotations

import pytest
from backend import auth, db, main
from fastapi.testclient import TestClient

POSITION = {"symbol": "AAPL", "broker": "schwab", "quantity": 10,
            "average_cost": 180, "current_price": 225}


@pytest.fixture
def client(tmp_path):
    db.set_db_path(tmp_path / "gate.db")
    db.init_db()
    yield TestClient(main.app)
    auth.set_request_gate(None)


def test_without_a_gate_nothing_changes(client):
    assert client.post("/api/positions", json=POSITION).status_code == 200


def test_a_denial_is_402_with_the_gates_own_words(client):
    auth.set_request_gate(
        lambda method, path: "Subscribe to do that." if method == "POST" else None
    )
    assert client.get("/api/portfolio").status_code == 200
    denied = client.post("/api/positions", json=POSITION)
    assert denied.status_code == 402
    assert denied.json()["detail"] == "Subscribe to do that."


def test_public_paths_never_reach_the_gate(client):
    """Version and the auth endpoints must answer even for an account the
    gate would refuse — sign-out and the billing portal live there, and a
    person who cannot reach them cannot fix the thing the gate is
    complaining about."""
    auth.set_request_gate(lambda method, path: "no")
    assert client.get("/api/v1/version").status_code == 200

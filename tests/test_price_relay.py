"""Prices posted in from a feed running outside this deployment.

Serin's providers run wherever Serin runs, which is not always where they
succeed: Yahoo answers a residential address and returns 429 to a datacenter
one, so the same code that fails on the server works on a machine at home.
The relay lets that machine do the fetching and post the result into the
shared cache.

Most of what these tests care about is the gate. An ingest anyone can post to
is a way to make every number on the dashboard wrong from the outside, and
wrong prices are worse than stale ones.
"""

from __future__ import annotations

import pytest
from backend import db, scope
from backend.config import settings
from backend.models import PositionIn
from fastapi.testclient import TestClient

TOKEN = "relay-secret-token"


@pytest.fixture
def client(tmp_path, monkeypatch):
    db.set_db_path(tmp_path / "relay.db")
    db.init_db()
    monkeypatch.setattr(settings, "price_relay_token", TOKEN)
    from backend.main import app

    return TestClient(app)


def auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def quotes(*rows):
    return {"quotes": [
        {"symbol": s, "price": p, "asset_type": a, "sector": sec}
        for s, p, a, sec in rows
    ]}


# --- the gate --------------------------------------------------------------


def test_a_post_without_a_token_is_refused(client):
    response = client.post("/api/v1/prices/ingest", json=quotes(("AAPL", 1.0, "stock", "")))
    assert response.status_code == 404


def test_a_wrong_token_looks_exactly_like_a_disabled_endpoint(client):
    """404 on purpose. A scanner must not be able to tell a wrong secret from
    an endpoint that is switched off — a 401 confirms the endpoint exists and
    that the guess was merely wrong."""
    wrong = client.post("/api/v1/prices/ingest", headers=auth("nope"),
                        json=quotes(("AAPL", 1.0, "stock", "")))
    assert wrong.status_code == 404

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(settings, "price_relay_token", "")
        disabled = client.post("/api/v1/prices/ingest", headers=auth(),
                               json=quotes(("AAPL", 1.0, "stock", "")))
    assert disabled.status_code == wrong.status_code
    assert disabled.json() == wrong.json()


def test_the_relay_is_off_unless_a_token_is_configured(client, monkeypatch):
    """An empty token disables it rather than accepting an empty one, which is
    the failure mode where the endpoint is open to the world."""
    monkeypatch.setattr(settings, "price_relay_token", "")
    for headers in ({}, auth(""), auth()):
        assert client.post("/api/v1/prices/ingest", headers=headers,
                           json=quotes(("AAPL", 1.0, "stock", ""))).status_code == 404


# --- what it does when let in ----------------------------------------------


def test_a_posted_price_lands_in_the_shared_cache(client):
    response = client.post("/api/v1/prices/ingest", headers=auth(),
                           json=quotes(("AAPL", 190.5, "stock", "Technology")))
    assert response.status_code == 200
    assert response.json()["written"] == 1

    with scope.using(scope.INSTANCE_SCOPE):
        cached = db.get_cached_quotes([("AAPL", "stock")])
    assert cached[("AAPL", "stock")][0] == pytest.approx(190.5)


def test_a_whole_book_posts_in_one_call(client):
    rows = [(f"S{i}", 100.0 + i, "stock", "Technology") for i in range(500)]
    response = client.post("/api/v1/prices/ingest", headers=auth(), json=quotes(*rows))
    assert response.json() == {"accepted": 500, "written": 500, "source": "relay"}


def test_an_oversized_post_is_refused_rather_than_truncated(client):
    rows = [(f"S{i}", 1.0, "stock", "") for i in range(2001)]
    assert client.post("/api/v1/prices/ingest", headers=auth(),
                       json=quotes(*rows)).status_code == 413


def test_a_zero_price_is_not_written(client):
    """A zero is absence, not a price. Writing it would freeze a holding at
    nothing, which is the one thing worse than a stale number."""
    response = client.post("/api/v1/prices/ingest", headers=auth(),
                           json=quotes(("AAPL", 0.0, "stock", ""),
                                       ("MSFT", 400.0, "stock", "")))
    assert response.json()["written"] == 1
    with scope.using(scope.INSTANCE_SCOPE):
        assert ("AAPL", "stock") not in db.get_cached_quotes([("AAPL", "stock")])


# --- what the relay asks for ----------------------------------------------


def test_tracked_lists_what_the_deployment_prices(client):
    db.create_position(PositionIn(symbol="AAPL", broker="manual", asset_type="stock",
                                  quantity=1, average_cost=1, current_price=1))
    response = client.get("/api/v1/prices/tracked", headers=auth())
    assert response.status_code == 200
    assert {"symbol": "AAPL", "asset_type": "stock"} in response.json()


def test_tracked_is_behind_the_same_gate(client):
    assert client.get("/api/v1/prices/tracked").status_code == 404
    assert client.get("/api/v1/prices/tracked", headers=auth("nope")).status_code == 404


# --- the point: it should replace provider calls, not add to them ----------


def test_the_sweep_skips_what_the_relay_is_keeping_current(client, monkeypatch):
    """Without this the relay only adds freshness between sweeps, and the
    provider bill is unchanged — the sweep would keep buying prices it already
    has."""
    from backend import prices

    db.create_position(PositionIn(symbol="AAPL", broker="manual", asset_type="stock",
                                  quantity=1, average_cost=1, current_price=1))
    client.post("/api/v1/prices/ingest", headers=auth(),
                json=quotes(("AAPL", 190.5, "stock", "Technology")))

    asked = []

    def never(positions):
        asked.extend(p.symbol for p in positions)
        return {"prices": {}, "errors": []}

    monkeypatch.setattr(prices.connectors, "market_data_chain",
                        lambda: [("fake", type("C", (), {"refresh_prices": staticmethod(never)})())])
    monkeypatch.setattr(prices, "_us_equity_market_open", lambda *a, **k: True)

    with scope.using(scope.INSTANCE_SCOPE):
        result = prices.refresh_tracked_quotes()

    assert asked == [], f"the sweep re-bought a price the relay had just posted: {asked}"
    assert result["skipped"] >= 1


# --- the session gate is not the relay's gate ------------------------------


def test_the_relay_paths_are_not_behind_the_session_lock():
    """A relay is a machine with a token, not a person with a cookie, so the
    session gate cannot admit it — and has nothing to add, since both paths
    refuse everything without the relay secret. Getting this wrong returns 401
    from middleware the handler never sees, which is what it did."""
    from backend import auth

    for path in ("/api/v1/prices/ingest", "/api/v1/prices/tracked",
                 "/api/prices/ingest", "/api/prices/tracked"):
        assert auth.is_public_path(path), path


def test_being_public_does_not_make_it_readable(client, monkeypatch):
    """The middleware steps aside; the endpoint's own secret does the work."""
    from backend import auth

    monkeypatch.setattr(auth, "_authorizer", None)
    assert client.get("/api/v1/prices/tracked").status_code == 404
    assert client.post("/api/v1/prices/ingest",
                       json=quotes(("AAPL", 1.0, "stock", ""))).status_code == 404

"""Accounts entity — schema, per-account rollups, API round-trip."""

from __future__ import annotations

from backend import db, main
from backend.models import AccountIn, PositionIn
from fastapi.testclient import TestClient


def _fresh(tmp_path):
    db.set_db_path(tmp_path / "acct.db")
    db.init_db()


def test_create_and_list_accounts(tmp_path):
    _fresh(tmp_path)
    db.create_account(AccountIn(name="Schwab Taxable", kind="taxable", broker="schwab"))
    db.create_account(AccountIn(name="Roth IRA", kind="roth_ira", broker="schwab"))
    accounts = db.list_accounts(with_summary=False)
    assert {a.name for a in accounts} == {"Schwab Taxable", "Roth IRA"}
    assert all(a.created_at for a in accounts)


def test_account_rollup_links_via_broker(tmp_path):
    _fresh(tmp_path)
    db.create_account(AccountIn(name="Schwab Taxable", kind="taxable", broker="schwab"))
    db.create_position(PositionIn(
        symbol="AAPL", broker="schwab", asset_type="stock",
        quantity=10, average_cost=180, current_price=225,
    ))
    db.create_position(PositionIn(
        symbol="CASH", broker="schwab", asset_type="cash",
        quantity=500, average_cost=1, current_price=1,
    ))
    accounts = db.list_accounts(with_summary=True)
    schwab = next(a for a in accounts if a.broker == "schwab")
    assert schwab.market_value == 10 * 225 + 500
    assert schwab.total_cost == 10 * 180
    assert schwab.cash_value == 500
    assert schwab.position_count == 1  # cash excluded from count


def test_account_api_round_trip(tmp_path):
    _fresh(tmp_path)
    client = TestClient(main.app)
    r = client.post("/api/accounts", json={"name": "Brokerage", "kind": "taxable", "broker": "fidelity"})
    assert r.status_code == 200
    listed = client.get("/api/v1/accounts").json()
    assert len(listed["accounts"]) == 1
    deleted = client.delete(f"/api/accounts/{listed['accounts'][0]['id']}")
    assert deleted.status_code == 200
    assert client.get("/api/accounts").json()["accounts"] == []


def test_blank_name_rejected(tmp_path):
    _fresh(tmp_path)
    client = TestClient(main.app)
    r = client.post("/api/accounts", json={"name": "   ", "kind": "taxable"})
    assert r.status_code == 422

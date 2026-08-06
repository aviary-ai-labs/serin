"""Coinbase holdings connector — signing, sync/reconcile, and endpoint guard."""

from __future__ import annotations

import hashlib
import hmac

import pytest
from backend import db
from backend.connectors.holdings import coinbase

ACCOUNTS = {
    "data": [
        {
            "currency": {"code": "BTC", "name": "Bitcoin", "type": "crypto"},
            "balance": {"amount": "0.5", "currency": "BTC"},
            "native_balance": {"amount": "30000.00", "currency": "USD"},
        },
        {
            "currency": {"code": "ETH", "name": "Ethereum", "type": "crypto"},
            "balance": {"amount": "4", "currency": "ETH"},
            "native_balance": {"amount": "12000.00", "currency": "USD"},
        },
        {  # fiat wallet — must be skipped
            "currency": {"code": "USD", "name": "US Dollar", "type": "fiat"},
            "balance": {"amount": "250.00", "currency": "USD"},
            "native_balance": {"amount": "250.00", "currency": "USD"},
        },
        {  # zero balance — must be skipped
            "currency": {"code": "DOGE", "name": "Dogecoin", "type": "crypto"},
            "balance": {"amount": "0", "currency": "DOGE"},
            "native_balance": {"amount": "0", "currency": "USD"},
        },
    ],
    "pagination": {"next_uri": None},
}


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError("should not reach raise_for_status in these tests")


@pytest.fixture
def isolated_db(tmp_path):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()
    yield


def _connector(cfg=None):
    return coinbase.CoinbaseConnector(cfg if cfg is not None else {"api_key": "k", "api_secret": "s"})


def _positions() -> dict[str, dict]:
    with db.connect() as conn:
        return {
            row["symbol"]: dict(row)
            for row in conn.execute(
                "SELECT symbol, quantity, current_price, source, broker, asset_type FROM positions"
            )
        }


def test_signing_scheme(monkeypatch):
    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        return FakeResp({"data": [], "pagination": {"next_uri": None}})

    monkeypatch.setattr(coinbase.httpx, "get", fake_get)
    monkeypatch.setattr(coinbase.time, "time", lambda: 1700000000)

    _connector({"api_key": "mykey", "api_secret": "mysecret"})._fetch_accounts()

    expected = hmac.new(
        b"mysecret", b"1700000000GET/v2/accounts?limit=100", hashlib.sha256
    ).hexdigest()
    assert captured["headers"]["CB-ACCESS-SIGN"] == expected
    assert captured["headers"]["CB-ACCESS-KEY"] == "mykey"
    assert captured["headers"]["CB-ACCESS-TIMESTAMP"] == "1700000000"
    assert captured["url"].endswith("/v2/accounts?limit=100")


def test_sync_reconciles_crypto_only(isolated_db, monkeypatch):
    monkeypatch.setattr(coinbase.httpx, "get", lambda *a, **k: FakeResp(ACCOUNTS))
    summary = _connector().sync()

    assert summary["accounts"] == 4
    assert summary["positions"] == 2  # BTC + ETH; USD (fiat) and DOGE (zero) skipped

    rows = _positions()
    assert set(rows) == {"BTC", "ETH"}
    assert rows["BTC"]["quantity"] == 0.5
    assert rows["BTC"]["current_price"] == 60000.0  # 30000 / 0.5, seeded from native_balance
    assert rows["ETH"]["current_price"] == 3000.0  # 12000 / 4
    assert rows["BTC"]["source"] == "coinbase"
    assert rows["BTC"]["broker"] == "coinbase"
    assert rows["BTC"]["asset_type"] == "crypto"


def test_resync_removes_sold_coins(isolated_db, monkeypatch):
    monkeypatch.setattr(coinbase.httpx, "get", lambda *a, **k: FakeResp(ACCOUNTS))
    _connector().sync()

    only_btc = {"data": [ACCOUNTS["data"][0]], "pagination": {"next_uri": None}}
    monkeypatch.setattr(coinbase.httpx, "get", lambda *a, **k: FakeResp(only_btc))
    summary = _connector().sync()

    assert summary["removed"] == 1
    assert set(_positions()) == {"BTC"}


def test_sync_never_touches_other_sources(isolated_db, monkeypatch):
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO positions
               (symbol, name, broker, asset_type, quantity, average_cost, current_price, sector, currency, source, updated_at)
               VALUES ('AAPL','Apple','manual','stock',10,100,150,'','USD','manual','2026-01-01T00:00:00Z')"""
        )
    monkeypatch.setattr(coinbase.httpx, "get", lambda *a, **k: FakeResp(ACCOUNTS))
    _connector().sync()

    rows = _positions()
    assert rows["AAPL"]["source"] == "manual"  # untouched
    assert set(rows) == {"AAPL", "BTC", "ETH"}


def test_test_requires_keys(isolated_db):
    result = _connector({}).test()
    assert result.ok is False
    assert "api key" in result.message.lower()


def test_test_ok_reports_wallet_count(isolated_db, monkeypatch):
    monkeypatch.setattr(coinbase.httpx, "get", lambda *a, **k: FakeResp(ACCOUNTS))
    result = _connector().test()
    assert result.ok is True
    assert "2 crypto" in result.message


def test_test_surfaces_bad_key(isolated_db, monkeypatch):
    monkeypatch.setattr(coinbase.httpx, "get", lambda *a, **k: FakeResp({}, status=401))
    result = _connector().test()
    assert result.ok is False
    assert "rejected" in result.message.lower()


def test_sync_endpoint_rejects_non_syncable(isolated_db):
    from backend.main import app
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        resp = client.post("/api/connectors/yahoo/sync")  # market_data, supports_sync=False
    assert resp.status_code == 400

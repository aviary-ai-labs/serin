"""Binance holdings connector — signing, sync/reconcile, and base-URL select."""

from __future__ import annotations

import hashlib
import hmac

import pytest
from backend import db
from backend.connectors.holdings import binance

ACCOUNT = {
    "balances": [
        {"asset": "BTC", "free": "0.5", "locked": "0.1"},  # qty 0.6
        {"asset": "ETH", "free": "4", "locked": "0"},       # qty 4
        {"asset": "USDT", "free": "0", "locked": "0"},      # zero → skip
    ]
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
    base = {"api_key": "k", "api_secret": "s"}
    if cfg:
        base.update(cfg)
    return binance.BinanceConnector(base)


def _positions() -> dict[str, dict]:
    with db.connect() as conn:
        return {
            row["symbol"]: dict(row)
            for row in conn.execute(
                "SELECT symbol, quantity, source, broker, asset_type FROM positions"
            )
        }


def test_signing_scheme(monkeypatch):
    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        return FakeResp({"balances": []})

    monkeypatch.setattr(binance.httpx, "get", fake_get)
    monkeypatch.setattr(binance.time, "time", lambda: 1700000000)

    _connector({"api_key": "mykey", "api_secret": "mysecret"})._fetch_balances()

    qs = "timestamp=1700000000000&recvWindow=10000"
    expected = hmac.new(b"mysecret", qs.encode(), hashlib.sha256).hexdigest()
    assert captured["headers"]["X-MBX-APIKEY"] == "mykey"
    assert captured["url"] == f"https://api.binance.com/api/v3/account?{qs}&signature={expected}"


def test_binance_us_base_url(monkeypatch):
    captured = {}
    monkeypatch.setattr(binance.httpx, "get", lambda url, **k: captured.update(url=url) or FakeResp({"balances": []}))
    monkeypatch.setattr(binance.time, "time", lambda: 1700000000)
    _connector({"base_url": "https://api.binance.us"})._fetch_balances()
    assert captured["url"].startswith("https://api.binance.us/api/v3/account?")


def test_sync_reconciles_balances(isolated_db, monkeypatch):
    monkeypatch.setattr(binance.httpx, "get", lambda *a, **k: FakeResp(ACCOUNT))
    summary = _connector().sync()

    assert summary["positions"] == 2  # BTC + ETH; USDT (zero) skipped
    rows = _positions()
    assert set(rows) == {"BTC", "ETH"}
    assert rows["BTC"]["quantity"] == pytest.approx(0.6)  # free + locked
    assert rows["ETH"]["quantity"] == 4
    assert rows["BTC"]["source"] == "binance"
    assert rows["BTC"]["broker"] == "binance"
    assert rows["BTC"]["asset_type"] == "crypto"


def test_resync_removes_sold(isolated_db, monkeypatch):
    monkeypatch.setattr(binance.httpx, "get", lambda *a, **k: FakeResp(ACCOUNT))
    _connector().sync()

    only_btc = {"balances": [ACCOUNT["balances"][0]]}
    monkeypatch.setattr(binance.httpx, "get", lambda *a, **k: FakeResp(only_btc))
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
    monkeypatch.setattr(binance.httpx, "get", lambda *a, **k: FakeResp(ACCOUNT))
    _connector().sync()

    rows = _positions()
    assert rows["AAPL"]["source"] == "manual"
    assert set(rows) == {"AAPL", "BTC", "ETH"}


def test_test_requires_keys(isolated_db):
    result = binance.BinanceConnector({}).test()
    assert result.ok is False
    assert "api key" in result.message.lower()


def test_test_ok_reports_asset_count(isolated_db, monkeypatch):
    monkeypatch.setattr(binance.httpx, "get", lambda *a, **k: FakeResp(ACCOUNT))
    result = _connector().test()
    assert result.ok is True
    assert "2 asset" in result.message


def test_test_surfaces_bad_key(isolated_db, monkeypatch):
    monkeypatch.setattr(binance.httpx, "get", lambda *a, **k: FakeResp({}, status=401))
    result = _connector().test()
    assert result.ok is False
    assert "rejected" in result.message.lower()

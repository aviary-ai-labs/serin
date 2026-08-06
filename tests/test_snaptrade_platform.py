"""Wave-3 connector platform: SnapTrade portal creds, transaction backfill,
per-connector auto-sync scheduling, in-app docs endpoint."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from backend import db, scheduler, snaptrade
from backend.config import settings
from backend.connectors import registry
from backend.main import app
from fastapi.testclient import TestClient


def _fresh(tmp_path):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()


# --- portal-first credentials -------------------------------------------------

def test_snaptrade_credentials_prefer_portal(tmp_path, monkeypatch):
    _fresh(tmp_path)
    monkeypatch.setattr(settings, "snaptrade_client_id", "")
    monkeypatch.setattr(settings, "snaptrade_consumer_key", "")
    assert snaptrade.snaptrade_available() is False

    registry.set_config("snaptrade", {"client_id": "portal-id", "consumer_key": "portal-key"})
    assert snaptrade.resolved_credentials() == ("portal-id", "portal-key")
    assert snaptrade.snaptrade_available() is True


def test_snaptrade_env_fallback(tmp_path, monkeypatch):
    _fresh(tmp_path)
    monkeypatch.setattr(settings, "snaptrade_client_id", "env-id")
    monkeypatch.setattr(settings, "snaptrade_consumer_key", "env-key")
    assert snaptrade.resolved_credentials() == ("env-id", "env-key")


# --- transaction backfill ------------------------------------------------------

def _fake_activities():
    return [
        {
            "id": "act-1",
            "type": "BUY",
            "symbol": {"symbol": "AAPL"},
            "units": 10,
            "price": 150.0,
            "fee": 1.0,
            "amount": -1501.0,
            "trade_date": "2026-05-02",
            "institution": "Robinhood",
            "currency": {"code": "USD"},
        },
        {
            "id": "act-2",
            "type": "DIVIDEND",
            "symbol": {"symbol": "MSFT"},
            "units": 0,
            "price": 0,
            "fee": 0,
            "amount": 12.5,
            "trade_date": "2026-05-10",
            "institution": "Robinhood",
            "currency": {"code": "USD"},
        },
        {
            "id": "act-3",
            "type": "SOMETHING_EXOTIC",
            "symbol": {"symbol": "???"},
            "units": 0,
            "price": 0,
            "fee": 0,
            "amount": 0,
            "trade_date": "2026-05-11",
            "institution": "Robinhood",
            "currency": {"code": "USD"},
        },
    ]


def _wire_fake_backfill(monkeypatch):
    monkeypatch.setattr(snaptrade, "get_stored_user", lambda: {"userId": "u", "userSecret": "s"})

    class FakeReporting:
        def get_activities(self, **kwargs):
            return SimpleNamespace(body=_fake_activities())

    class FakeClient:
        transactions_and_reporting = FakeReporting()

    monkeypatch.setattr(snaptrade, "_get_client", lambda: FakeClient())


def test_backfill_maps_and_is_idempotent(tmp_path, monkeypatch):
    _fresh(tmp_path)
    _wire_fake_backfill(monkeypatch)

    first = snaptrade.backfill_transactions(days=365)
    assert first["imported"] == 2          # buy + dividend
    assert first["skipped_unknown"] == 1   # exotic type is counted, not guessed

    rows = db.list_transactions(limit=100)
    by_symbol = {t.symbol: t for t in rows}
    assert by_symbol["AAPL"].action == "buy"
    assert by_symbol["AAPL"].quantity == 10
    assert by_symbol["AAPL"].notes == "snaptrade:act-1"
    assert by_symbol["AAPL"].source == "snaptrade"
    # Dividend cash amount rides the price field (Serin convention).
    assert by_symbol["MSFT"].action == "dividend"
    assert by_symbol["MSFT"].amount == 12.5

    second = snaptrade.backfill_transactions(days=365)
    assert second["imported"] == 0
    assert second["skipped_existing"] == 2
    assert len(db.list_transactions(limit=100)) == 2


# --- per-connector auto-sync ----------------------------------------------------

def test_auto_sync_runs_once_per_day(tmp_path, monkeypatch):
    _fresh(tmp_path)
    registry.set_enabled("snaptrade", True)
    registry.set_config("snaptrade", {"auto_sync_daily": True})

    calls = []

    class FakeConnector:
        supports_sync = True

        def sync(self):
            calls.append(datetime.now(UTC))
            return {"positions": 0}

    monkeypatch.setattr(scheduler, "_instantiate_for_test", None, raising=False)
    import backend.connectors.registry as reg
    monkeypatch.setattr(reg, "instantiate", lambda connector_id: FakeConnector() if connector_id == "snaptrade" else None)

    synced_first = asyncio.run(scheduler.maybe_auto_sync_connectors())
    synced_second = asyncio.run(scheduler.maybe_auto_sync_connectors())

    assert synced_first == ["snaptrade"]
    assert synced_second == []  # once per day
    assert len(calls) == 1


def test_auto_sync_skips_when_disabled(tmp_path, monkeypatch):
    _fresh(tmp_path)
    registry.set_enabled("snaptrade", True)
    registry.set_config("snaptrade", {"auto_sync_daily": False})

    import backend.connectors.registry as reg

    def must_not_instantiate(connector_id):
        raise AssertionError("should not instantiate when auto_sync_daily is off")

    monkeypatch.setattr(reg, "instantiate", must_not_instantiate)
    assert asyncio.run(scheduler.maybe_auto_sync_connectors()) == []


# --- in-app connector docs -------------------------------------------------------

def test_connector_docs_endpoint(tmp_path):
    _fresh(tmp_path)
    client = TestClient(app)

    payload = client.get("/api/connectors/coingecko/docs")
    assert payload.status_code == 200
    body = payload.json()
    assert body["id"] == "coingecko"
    assert "crypto" in body["markdown"].lower()
    # Section extraction stops before the next connector's heading.
    assert "snaptrade —" not in body["markdown"].lower()

    missing = client.get("/api/connectors/nope/docs")
    assert missing.status_code == 404

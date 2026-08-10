from __future__ import annotations

import types

import pytest
from backend import db, snaptrade
from backend.config import settings
from backend.main import app
from backend.models import PositionIn
from fastapi.testclient import TestClient


@pytest.fixture
def fresh_db(tmp_path):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()
    return db


# ---------------------------------------------------------------------------
# Pure helpers


def test_slug_broker_matches_existing_tags():
    assert snaptrade._slug_broker("E*TRADE") == "etrade"
    assert snaptrade._slug_broker("Robinhood") == "robinhood"
    assert snaptrade._slug_broker("Charles Schwab") == "charlesschwab"


def test_asset_type_mapping():
    assert snaptrade._asset_type("cs") == "stock"
    assert snaptrade._asset_type("et") == "etf"
    assert snaptrade._asset_type("crypto") == "crypto"
    assert snaptrade._asset_type("oe") == "option"
    assert snaptrade._asset_type(None) == "stock"


def test_nested_accessor_handles_missing():
    obj = {"symbol": {"symbol": {"symbol": "AAPL"}}}
    assert snaptrade._g(obj, "symbol", "symbol", "symbol") == "AAPL"
    assert snaptrade._g(obj, "symbol", "missing", "x", default="?") == "?"


# ---------------------------------------------------------------------------
# Reconciliation (db layer)


def test_replace_synced_positions_upsert_and_cleanup(fresh_db):
    rows = [
        PositionIn(symbol="AAPL", broker="robinhood", asset_type="stock", quantity=10, average_cost=150, current_price=200),
        PositionIn(symbol="CASH", broker="robinhood", asset_type="cash", quantity=500, average_cost=1, current_price=1),
    ]
    result = db.replace_synced_positions(rows, {"robinhood"})
    assert result["upserted"] == 2
    assert result["new_symbols"] == ["AAPL"]  # cash is excluded from repricing
    symbols = {p.symbol for p in db.list_positions()}
    assert {"AAPL", "CASH"} <= symbols

    # Next sync: AAPL sold, NVDA appears -> AAPL removed, NVDA added.
    rows2 = [
        PositionIn(symbol="NVDA", broker="robinhood", asset_type="stock", quantity=5, average_cost=100, current_price=120),
        PositionIn(symbol="CASH", broker="robinhood", asset_type="cash", quantity=900, average_cost=1, current_price=1),
    ]
    result2 = db.replace_synced_positions(rows2, {"robinhood"})
    assert result2["removed"] == 1
    by_symbol = {p.symbol: p for p in db.list_positions()}
    assert "AAPL" not in by_symbol
    assert "NVDA" in by_symbol
    assert by_symbol["CASH"].quantity == 900


def test_resync_preserves_market_data_price_and_sector(fresh_db):
    # First sync seeds the broker price for a brand-new holding.
    db.replace_synced_positions(
        [PositionIn(symbol="AAPL", broker="robinhood", asset_type="stock", quantity=10, average_cost=150, current_price=200)],
        {"robinhood"},
    )
    assert {p.symbol: p for p in db.list_positions()}["AAPL"].current_price == 200

    # Market data refresh then owns price + sector.
    db.update_prices({"AAPL": (211.0, "Technology")})

    # A later sync with a staler broker price must update holdings but NOT
    # revert the fresh market-data price or wipe the sector.
    db.replace_synced_positions(
        [PositionIn(symbol="AAPL", broker="robinhood", asset_type="stock", quantity=12, average_cost=150, current_price=205)],
        {"robinhood"},
    )
    aapl = {p.symbol: p for p in db.list_positions()}["AAPL"]
    assert aapl.current_price == 211.0  # market-data price preserved
    assert aapl.quantity == 12          # holdings updated from broker
    assert aapl.sector == "Technology"  # market-data sector preserved


def test_sync_never_touches_manual_positions(fresh_db):
    db.create_position(PositionIn(symbol="TSLA", broker="manual", asset_type="stock", quantity=3, average_cost=200, current_price=400))
    db.replace_synced_positions(
        [PositionIn(symbol="AAPL", broker="robinhood", asset_type="stock", quantity=10, average_cost=150, current_price=200)],
        {"robinhood"},
    )
    # A second sync that no longer includes any robinhood rows must not delete TSLA.
    db.replace_synced_positions([], {"robinhood"})
    by_symbol = {p.symbol: p for p in db.list_positions()}
    assert "TSLA" in by_symbol
    assert by_symbol["TSLA"].source == "manual"
    assert "AAPL" not in by_symbol


def test_disconnect_scoped_to_broker(fresh_db):
    db.replace_synced_positions(
        [
            PositionIn(symbol="AAPL", broker="robinhood", asset_type="stock", quantity=1, average_cost=1, current_price=2),
            PositionIn(symbol="GOOG", broker="etrade", asset_type="stock", quantity=1, average_cost=1, current_price=2),
        ],
        {"robinhood", "etrade"},
    )
    removed = db.delete_positions_for_brokers({"robinhood"})
    assert removed == 1
    brokers = {p.broker for p in db.list_positions()}
    assert brokers == {"etrade"}


# ---------------------------------------------------------------------------
# Provider with a fake SDK client


class FakeResponse:
    def __init__(self, body):
        self.body = body


def _fake_client(positions=None, balances=None):
    auth = types.SimpleNamespace(
        register_snap_trade_user=lambda user_id=None: FakeResponse({"userId": user_id, "userSecret": "secret-xyz"}),
        login_snap_trade_user=lambda **kw: FakeResponse({"redirectURI": "https://app.snaptrade.com/connect/abc"}),
    )
    account_info = types.SimpleNamespace(
        list_user_accounts=lambda **kw: FakeResponse([
            {"id": "acc-1", "name": "Individual", "institution_name": "Robinhood", "balance": {"total": {"amount": 1500}}},
        ]),
        get_all_account_positions=lambda **kw: FakeResponse(positions if positions is not None else [
            {"symbol": {"symbol": {"symbol": "AAPL", "description": "Apple", "type": {"code": "cs"}}},
             "units": 10, "average_purchase_price": 150, "price": 200},
        ]),
        get_user_account_balance=lambda **kw: FakeResponse(balances if balances is not None else [
            {"currency": {"code": "USD"}, "cash": 500},
        ]),
    )
    connections = types.SimpleNamespace(
        list_brokerage_authorizations=lambda **kw: FakeResponse([
            {"id": "auth-1", "brokerage": {"name": "Robinhood"}, "disabled": False, "created_date": "2026-06-10"},
        ]),
        remove_brokerage_authorization=lambda **kw: FakeResponse(None),
    )
    return types.SimpleNamespace(authentication=auth, account_information=account_info, connections=connections)


@pytest.fixture
def configured(monkeypatch, fresh_db):
    monkeypatch.setattr(settings, "snaptrade_client_id", "cid")
    monkeypatch.setattr(settings, "snaptrade_consumer_key", "ckey")
    # Clear any pre-provisioned creds from the developer's real .env so the
    # standard registration path is exercised deterministically.
    monkeypatch.setattr(settings, "snaptrade_user_id", "")
    monkeypatch.setattr(settings, "snaptrade_user_secret", "")
    monkeypatch.setattr(snaptrade, "_client", _fake_client())
    return monkeypatch


def test_register_user_persists_secret(configured):
    user = snaptrade.get_or_register_user()
    assert user["userSecret"] == "secret-xyz"
    # Second call reuses the stored user rather than re-registering.
    assert snaptrade.get_or_register_user()["userId"] == user["userId"]


def test_personal_tier_uses_preprovisioned_user(monkeypatch, fresh_db):
    # Personal keys: registerUser would error, so the SDK must not be called.
    monkeypatch.setattr(settings, "snaptrade_client_id", "cid")
    monkeypatch.setattr(settings, "snaptrade_consumer_key", "ckey")
    monkeypatch.setattr(settings, "snaptrade_user_id", "personal-user-1")
    monkeypatch.setattr(settings, "snaptrade_user_secret", "preprovisioned-secret")

    def _boom(**kwargs):
        raise AssertionError("registerUser must not be called for personal keys")

    fake = _fake_client()
    fake.authentication.register_snap_trade_user = _boom
    monkeypatch.setattr(snaptrade, "_client", fake)

    user = snaptrade.get_or_register_user()
    assert user["userId"] == "personal-user-1"
    assert user["userSecret"] == "preprovisioned-secret"


def test_connection_portal_url_read_only(configured):
    url = snaptrade.connection_portal_url()
    assert url.startswith("https://app.snaptrade.com/connect/")


def test_sync_writes_positions_and_cash(configured):
    snaptrade.get_or_register_user()  # connect happens before any sync
    summary = snaptrade.sync(refresh_prices_after=False)  # skip the live market-data call
    assert summary["accounts"] == 1
    assert summary["positions"] == 2  # AAPL + CASH
    by_symbol = {p.symbol: p for p in db.list_positions()}
    assert by_symbol["AAPL"].source == "snaptrade"
    assert by_symbol["AAPL"].broker == "robinhood"
    assert by_symbol["AAPL"].quantity == 10
    assert by_symbol["CASH"].asset_type == "cash"
    assert by_symbol["CASH"].quantity == 500


def test_account_rows_handles_results_envelope(configured):
    fake = _fake_client(
        positions={
            "results": [
                {
                    "instrument": {
                        "kind": "crypto",
                        "symbol": "BTC",
                        "description": "Bitcoin",
                    },
                    "units": "0.25",
                    "price": "64000",
                    "cost_basis": "80000",
                },
                {
                    "instrument": {
                        "kind": "option",
                        "symbol": "MSFT  261218C00430000",
                        "description": "Microsoft Call",
                    },
                    "units": "2",
                    "price": "2620",
                    "cost_basis": "4329",
                }
            ],
            "data_freshness": {"as_of": "2026-06-12T00:00:00Z"},
        },
        balances=[{"currency": {"code": "USD"}, "cash": "125.50"}],
    )
    configured.setattr(snaptrade, "_client", fake)
    snaptrade.get_or_register_user()

    rows, broker = snaptrade._account_rows({"id": "acc-1", "institution": "Robinhood"})

    assert broker == "robinhood"
    btc = next(row for row in rows if row.symbol == "BTC")
    assert btc.name == "Bitcoin"
    assert btc.asset_type == "crypto"
    assert btc.quantity == 0.25
    assert btc.average_cost == 80000
    assert btc.current_price == 64000
    option = next(row for row in rows if row.asset_type == "option")
    assert option.symbol == "MSFT-261218-C430"
    assert option.quantity == 2
    assert option.average_cost == 43.29
    assert option.current_price == 26.2
    cash = next(row for row in rows if row.symbol == "CASH")
    assert cash.quantity == 125.50


def test_aggregate_rows_sums_cash_and_weights_cost():
    rows = [
        PositionIn(symbol="CASH", broker="robinhood", asset_type="cash", quantity=3000, average_cost=1, current_price=1),
        PositionIn(symbol="CASH", broker="robinhood", asset_type="cash", quantity=4000, average_cost=1, current_price=1),
        PositionIn(symbol="AAPL", broker="robinhood", asset_type="stock", quantity=10, average_cost=100, current_price=200),
        PositionIn(symbol="AAPL", broker="robinhood", asset_type="stock", quantity=10, average_cost=200, current_price=200),
    ]
    merged = {(r.symbol, r.asset_type): r for r in snaptrade._aggregate_rows(rows)}
    assert merged[("CASH", "cash")].quantity == 7000
    aapl = merged[("AAPL", "stock")]
    assert aapl.quantity == 20
    assert aapl.average_cost == 150  # quantity-weighted (100*10 + 200*10) / 20


def test_status_unconfigured(monkeypatch, fresh_db):
    monkeypatch.setattr(settings, "snaptrade_client_id", "")
    monkeypatch.setattr(settings, "snaptrade_consumer_key", "")
    status = snaptrade.status()
    assert status["configured"] is False
    assert status["registered"] is False


def test_broker_status_endpoint_unconfigured(monkeypatch, fresh_db):
    monkeypatch.setattr(settings, "snaptrade_client_id", "")
    client = TestClient(app)
    response = client.get("/api/broker/status")
    assert response.status_code == 200
    assert response.json()["configured"] is False


def test_connect_endpoint_503_when_unconfigured(monkeypatch, fresh_db):
    monkeypatch.setattr(settings, "snaptrade_client_id", "")
    client = TestClient(app)
    assert client.post("/api/broker/connect", json={}).status_code == 503


# ---------------------------------------------------------------------------
# Error surfacing


class _FakeApiException(Exception):
    """Shaped like snaptrade_client.exceptions.ApiException: str() is the whole
    HTTP exchange, and the useful sentence is buried in .body."""

    def __init__(self, status, body, headers):
        self.status = status
        self.body = body
        self.headers = headers
        super().__init__(f"({status})\nReason: Forbidden\nHTTP response headers: {headers}\nHTTP response body: {body}")


def test_rejected_credentials_read_as_a_credentials_problem():
    exc = _FakeApiException(
        403,
        {"detail": "Authentication credentials were not provided.", "status_code": 403},
        {"X-RateLimit-Limit": "250", "X-Request-ID": "491d4ea"},
    )
    message = snaptrade.error_message(exc)
    assert "credentials" in message.lower()
    assert "SnapTrade dashboard" in message
    # None of the transcript leaks through
    assert "X-RateLimit" not in message
    assert "X-Request-ID" not in message
    assert "\n" not in message


def test_transient_and_unknown_failures_stay_one_line():
    assert "try again" in snaptrade.error_message(_FakeApiException(503, {}, {})).lower()
    assert "rate-limiting" in snaptrade.error_message(_FakeApiException(429, {}, {})).lower()
    plain = snaptrade.error_message(RuntimeError("socket exploded\nstack line\nstack line"))
    assert plain == "socket exploded"


def test_json_string_bodies_are_parsed_too():
    exc = _FakeApiException(418, '{"detail": "I am a teapot"}', {})
    assert snaptrade.error_message(exc) == "SnapTrade returned HTTP 418: I am a teapot"

"""Multi-currency: FX conversion, position aggregation, display setting."""

from __future__ import annotations

from backend import db, fx
from backend.main import app
from backend.models import PositionIn
from fastapi.testclient import TestClient


def _fresh(tmp_path):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()


def test_convert_factor_math():
    rates = {"USD": 1.0, "EUR": 0.8, "GBP": 0.5}
    assert fx.convert_factor("USD", "USD", rates) == 1.0
    # 100 EUR -> USD: EUR rate 0.8 per USD => 125 USD
    assert fx.convert_factor("EUR", "USD", rates) == 1.0 / 0.8
    # USD -> GBP
    assert fx.convert_factor("USD", "GBP", rates) == 0.5
    # Missing rate degrades to 1.0 rather than blowing up the dashboard.
    assert fx.convert_factor("XXX", "USD", rates) == 1.0


def test_rates_cached_and_stale_served_on_failure(tmp_path, monkeypatch):
    _fresh(tmp_path)
    monkeypatch.setattr(fx, "_fetch_rates", lambda: {"USD": 1.0, "EUR": 0.9})
    assert fx.get_rates()["EUR"] == 0.9

    # Next call within TTL serves the cache without fetching.
    def must_not_fetch():
        raise AssertionError("fetch should not run inside TTL")

    monkeypatch.setattr(fx, "_fetch_rates", must_not_fetch)
    assert fx.get_rates()["EUR"] == 0.9

    # Forced refresh with a dead network serves the stale cache.
    monkeypatch.setattr(fx, "_fetch_rates", lambda: None)
    assert fx.get_rates(force=True)["EUR"] == 0.9


def test_eur_position_aggregates_into_usd_display(tmp_path, monkeypatch):
    _fresh(tmp_path)
    # 1 USD = 0.8 EUR  =>  1 EUR = 1.25 USD
    monkeypatch.setattr(fx, "_fetch_rates", lambda: {"USD": 1.0, "EUR": 0.8})

    db.create_position(
        PositionIn(symbol="SAP", broker="manual", asset_type="stock",
                   quantity=10, average_cost=100, current_price=120, currency="EUR")
    )
    db.create_position(
        PositionIn(symbol="AAPL", broker="manual", asset_type="stock",
                   quantity=1, average_cost=100, current_price=200, currency="USD")
    )

    summary = db.portfolio_summary()
    positions = {p.symbol: p for p in summary.positions}

    # SAP: 10 × €120 = €1200 → $1500 display; native price stays 120.
    assert positions["SAP"].market_value == 1500.0
    assert positions["SAP"].current_price == 120.0
    assert positions["SAP"].currency == "EUR"
    # Totals are display-currency sums.
    assert summary.total_value == 1500.0 + 200.0
    # Gain% is FX-invariant for same-currency cost/value.
    assert round(positions["SAP"].unrealized_gain_pct, 4) == 20.0


def test_all_usd_portfolio_never_touches_fx_network(tmp_path, monkeypatch):
    _fresh(tmp_path)

    def must_not_fetch():
        raise AssertionError("all-USD portfolios must not fetch FX")

    monkeypatch.setattr(fx, "_fetch_rates", must_not_fetch)
    db.create_position(PositionIn(symbol="AAPL", broker="manual", asset_type="stock", quantity=1, average_cost=1, current_price=2))
    summary = db.portfolio_summary()
    assert summary.total_value == 2.0


def test_display_currency_setting_endpoint(tmp_path):
    _fresh(tmp_path)
    client = TestClient(app)

    response = client.put("/api/settings/display-currency", json={"currency": "eur"})
    assert response.status_code == 200
    assert response.json()["display_currency"] == "EUR"
    assert fx.display_currency() == "EUR"

    bad = client.put("/api/settings/display-currency", json={"currency": "EURO"})
    assert bad.status_code == 400


def test_currency_roundtrips_through_position_api(tmp_path):
    _fresh(tmp_path)
    client = TestClient(app)
    created = client.post(
        "/api/positions",
        json={"symbol": "SAP", "broker": "manual", "asset_type": "stock",
              "quantity": 1, "average_cost": 100, "current_price": 110, "currency": "eur"},
    )
    assert created.status_code in (200, 201)
    assert created.json()["currency"] == "EUR"

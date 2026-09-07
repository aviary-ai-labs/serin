from __future__ import annotations

from backend import db, main, prices
from backend.config import settings
from backend.models import PositionIn
from backend.prices import fmp_symbol
from backend.providers import fmp as fmp_provider
from fastapi.testclient import TestClient


def test_fmp_crypto_symbol_removes_dash():
    assert fmp_symbol("BTC-USD", "crypto") == "BTCUSD"
    assert fmp_symbol("aapl", "stock") == "AAPL"


def test_fmp_bare_crypto_symbol_gets_usd_suffix():
    """Bare crypto tickers must map to the USD pair — 'BTC' alone resolves to
    an unrelated equity on FMP and returns a wildly wrong price."""
    assert fmp_symbol("BTC", "crypto") == "BTCUSD"
    assert fmp_symbol("eth", "crypto") == "ETHUSD"
    # Equities named like crypto are untouched.
    assert fmp_symbol("BTC", "stock") == "BTC"


def test_refresh_prices_endpoint_accepts_target_symbols(monkeypatch):
    captured = {}

    def fake_refresh_prices(symbols=None):
        captured["symbols"] = symbols
        return {"updated": 2, "symbols": sorted(symbols or []), "errors": []}

    monkeypatch.setattr(main, "refresh_prices", fake_refresh_prices)
    response = TestClient(main.app).post(
        "/api/prices/refresh",
        json={"symbols": [" aapl ", "MSFT", ""]},
    )

    assert response.status_code == 200
    assert captured["symbols"] == {"AAPL", "MSFT"}
    assert response.json()["symbols"] == ["AAPL", "MSFT"]


def test_missing_fmp_key_returns_visible_market_data_error(tmp_path, monkeypatch):
    """A provider the user chose and did not configure has to say so.

    Quotes fall through the chain now, so a later provider may well cover for
    the broken one — which is the point of a chain, and also exactly how a
    misconfigured key goes unnoticed for months. The complaint survives the
    fallthrough."""
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()
    db.create_position(PositionIn(symbol="AAPL", broker="manual", asset_type="stock", quantity=1))
    monkeypatch.setattr(settings, "market_data_provider", "fmp")
    monkeypatch.setattr(settings, "fmp_api_key", "")
    # FMP alone in the chain, so the test is about FMP and not about whichever
    # keyless backstop happens to be enabled.
    from backend.providers import fmp as fmp_provider

    monkeypatch.setattr(
        prices.connectors, "market_data_chain",
        lambda: [("fmp", fmp_provider.FMPProvider(api_key=""))],
    )

    result = prices.refresh_prices({"AAPL"})

    assert result["updated"] == 0
    assert result["errors"]
    # Message should hint at both setting an FMP key and the free Yahoo fallback.
    assert "FMP" in result["errors"][0] or "yahoo" in result["errors"][0].lower()


def test_refresh_prices_uses_fmp_when_configured(tmp_path, monkeypatch):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()
    db.create_position(
        PositionIn(
            symbol="AAPL",
            broker="manual",
            asset_type="stock",
            quantity=2,
            average_cost=100,
            current_price=0,
        )
    )
    monkeypatch.setattr(settings, "market_data_provider", "fmp")
    monkeypatch.setattr(settings, "fmp_api_key", "test-key")

    def fake_fmp_get(path, params, *args, **kwargs):
        # The sweep now asks the quote endpoint for the whole book first and
        # only falls to the profile for a symbol whose sector it lacks.
        if path == "stable/quote":
            return [{"symbol": s, "price": 212.5}
                    for s in params["symbol"].split(",")], None
        if path == "stable/profile":
            assert params["symbol"] == "AAPL"
            return [{"price": 212.5, "sector": "Technology"}], None
        raise AssertionError(path)

    # _get is parameterized by api_key/base_url now (portal-configurable), so the
    # fake accepts the extra positional args the connector passes through.
    monkeypatch.setattr(fmp_provider, "_get", fake_fmp_get)

    result = prices.refresh_prices({"AAPL"})
    aapl = db.list_positions()[0]

    assert result["provider"] == "fmp"
    assert result["updated"] == 1
    assert result["symbols"] == ["AAPL"]
    assert result["errors"] == []
    assert aapl.current_price == 212.5
    assert aapl.sector == "Technology"


def test_fetch_price_history_uses_fmp_when_configured(tmp_path, monkeypatch):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()
    db.create_position(
        PositionIn(
            symbol="AAPL",
            broker="manual",
            asset_type="stock",
            quantity=2,
            average_cost=100,
            current_price=200,
        )
    )
    monkeypatch.setattr(settings, "market_data_provider", "fmp")
    monkeypatch.setattr(settings, "fmp_api_key", "test-key")

    def fake_fmp_get(path, params, *args, **kwargs):
        assert path == "stable/historical-price-eod/light"
        assert params["symbol"] == "AAPL"
        return [
            {"date": "2026-06-12", "price": 211.5},
            {"date": "2026-06-11", "price": 209.0},
        ], None

    monkeypatch.setattr(fmp_provider, "_get", fake_fmp_get)

    result = prices.fetch_price_history("1w")

    assert result["errors"] == []
    assert result["provider"] == "fmp"
    assert result["history"]["AAPL"]["dates"] == ["2026-06-11", "2026-06-12"]
    assert result["history"]["AAPL"]["closes"] == [209.0, 211.5]

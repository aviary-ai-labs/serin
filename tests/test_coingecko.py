"""CoinGecko provider + crypto-layer routing tests."""

from __future__ import annotations

from unittest.mock import MagicMock

from backend import connectors, db, prices
from backend.connectors import registry
from backend.models import Position, PositionIn
from backend.providers import coingecko


def _position(symbol="BTC", asset_type="crypto", quantity=1.0, current_price=60000.0):
    return Position(
        id=1, symbol=symbol, name=symbol, broker="manual", asset_type=asset_type,
        quantity=quantity, average_cost=50000.0, current_price=current_price,
        sector="", market_value=quantity * current_price, total_cost=quantity * 50000.0,
        unrealized_gain=0.0, unrealized_gain_pct=0.0,
    )


def test_norm_strips_usd_pairs():
    assert coingecko._norm("BTC") == "btc"
    assert coingecko._norm("BTC-USD") == "btc"
    assert coingecko._norm("BTCUSD") == "btc"
    assert coingecko._norm("ETH-USDT") == "eth"


def test_refresh_prices_uses_markets_endpoint(monkeypatch):
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = [
            {"id": "bitcoin", "symbol": "btc", "current_price": 60123.45},
        ]
        return response

    monkeypatch.setattr("httpx.get", fake_get)

    provider = coingecko.CoinGeckoProvider()
    result = provider.refresh_prices([_position("BTC")])

    assert result["prices"]["BTC"] == (60123.45, "Crypto")
    assert result["errors"] == []
    assert "/coins/markets" in captured["url"]
    assert captured["params"]["vs_currency"] == "usd"


def test_fetch_history_parses_market_chart(monkeypatch):
    def fake_get(url, params=None, headers=None, timeout=None):
        response = MagicMock()
        response.raise_for_status.return_value = None
        if "market_chart" in url:
            response.json.return_value = {
                "prices": [
                    [1781481600000, 59000.0],
                    [1781568000000, 60000.0],
                    [1781568000000 + 3600_000, 60100.0],  # same-day intraday dupe
                ]
            }
        else:
            response.json.return_value = [{"id": "bitcoin", "symbol": "btc", "current_price": 60000.0}]
        return response

    monkeypatch.setattr("httpx.get", fake_get)

    provider = coingecko.CoinGeckoProvider()
    result = provider.fetch_history("1m", ["BTC"], {"BTC": _position("BTC")})

    assert result["errors"] == []
    series = result["history"]["BTC"]
    assert len(series["dates"]) == 2  # deduped to one close per day
    assert series["closes"][-1] == 60100.0


def test_fetch_history_rejects_non_crypto():
    provider = coingecko.CoinGeckoProvider()
    result = provider.fetch_history("1m", ["AAPL"], {"AAPL": _position("AAPL", "stock", 1, 100.0)})
    assert result["history"] == {}
    assert any("crypto only" in err for err in result["errors"])


def test_coingecko_never_elected_main_provider(tmp_path, monkeypatch):
    """Enabling CoinGecko alone must not make it the main price source."""
    from backend.config import settings

    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()
    monkeypatch.setattr(settings, "market_data_provider", "auto")
    monkeypatch.setattr(settings, "fmp_api_key", "")
    registry.set_enabled("coingecko", True)

    assert connectors.active_market_data_id() == "yahoo"  # env auto fallback
    layer = connectors.active_crypto_data()
    assert layer is not None and layer.manifest.id == "coingecko"


def test_refresh_routes_crypto_to_layer(tmp_path, monkeypatch):
    """With the layer enabled, crypto refresh goes to CoinGecko and equities
    to the main provider."""
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()
    db.create_position(PositionIn(symbol="BTC", broker="manual", asset_type="crypto", quantity=1))
    db.create_position(PositionIn(symbol="AAPL", broker="manual", asset_type="stock", quantity=1))

    class FakeMain:
        def refresh_prices(self, positions):
            assert all(p.asset_type != "crypto" for p in positions)
            return {"prices": {"AAPL": (190.0, "Technology")}, "errors": []}

    class FakeCrypto:
        def refresh_prices(self, positions):
            assert all(p.asset_type == "crypto" for p in positions)
            return {"prices": {"BTC": (61000.0, "Crypto")}, "errors": []}

    monkeypatch.setattr(prices.connectors, "active_market_data", lambda: FakeMain())
    monkeypatch.setattr(prices.connectors, "active_market_data_id", lambda: "yahoo")
    monkeypatch.setattr(prices.connectors, "active_crypto_data", lambda: FakeCrypto())

    result = prices.refresh_prices()

    assert sorted(result["symbols"]) == ["AAPL", "BTC"]
    assert result["errors"] == []
    assert result["provider"] == "yahoo+coingecko"
    by_symbol = {p.symbol: p for p in db.list_positions()}
    assert by_symbol["BTC"].current_price == 61000.0
    assert by_symbol["AAPL"].current_price == 190.0

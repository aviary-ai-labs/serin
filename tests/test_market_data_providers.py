"""Tests for the market-data provider package (Yahoo + FMP) and the
orchestrator dispatch in backend.prices.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from backend.config import settings
from backend.models import Position
from backend.providers import yahoo


@pytest.fixture
def reset_provider(monkeypatch):
    """Reset provider-related env so tests have a known starting state."""
    monkeypatch.setattr(settings, "market_data_provider", "auto")
    monkeypatch.setattr(settings, "fmp_api_key", "")
    return monkeypatch


def _position(symbol="AAPL", asset_type="stock", quantity=10, current_price=200.0):
    return Position(
        id=1,
        symbol=symbol,
        name=symbol,
        broker="manual",
        asset_type=asset_type,
        quantity=quantity,
        average_cost=100.0,
        current_price=current_price,
        sector="",
        market_value=quantity * current_price,
        total_cost=quantity * 100.0,
        unrealized_gain=quantity * (current_price - 100.0),
        unrealized_gain_pct=((current_price - 100.0) / 100.0) * 100,
    )


# ---------- provider resolution ----------

def test_auto_falls_back_to_yahoo_when_fmp_unconfigured(reset_provider):
    assert settings.resolved_market_data_provider == "yahoo"


def test_auto_prefers_fmp_when_configured(reset_provider):
    reset_provider.setattr(settings, "fmp_api_key", "sk-fmp")
    assert settings.resolved_market_data_provider == "fmp"


def test_explicit_yahoo_always_resolves(reset_provider):
    reset_provider.setattr(settings, "market_data_provider", "yahoo")
    assert settings.resolved_market_data_provider == "yahoo"


def test_explicit_fmp_without_key_is_none(reset_provider):
    reset_provider.setattr(settings, "market_data_provider", "fmp")
    assert settings.resolved_market_data_provider == "none"


# ---------- yahoo provider ----------

def test_yahoo_symbol_normalizes_crypto():
    assert yahoo._symbol("BTCUSD", "crypto") == "BTC-USD"
    assert yahoo._symbol("BTC-USD", "crypto") == "BTC-USD"
    assert yahoo._symbol("AAPL", "stock") == "AAPL"


def test_yahoo_refresh_uses_chart_endpoint(monkeypatch):
    """The Yahoo provider should ask the chart endpoint and pull
    regularMarketPrice from meta."""

    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None, follow_redirects=False):
        captured["url"] = url
        captured["params"] = params
        response = MagicMock()
        response.status_code = 200
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "chart": {
                "result": [
                    {
                        "meta": {
                            "regularMarketPrice": 230.5,
                            "previousClose": 225.0,
                            "currency": "USD",
                        },
                        "timestamp": [1718000000, 1718086400],
                        "indicators": {"quote": [{"close": [225.0, 230.5]}]},
                    }
                ]
            }
        }
        return response

    monkeypatch.setattr("httpx.get", fake_get)

    provider = yahoo.YahooProvider()
    result = provider.refresh_prices([_position("AAPL", "stock", 10, 200.0)])

    assert "AAPL" in result["prices"]
    price, _sector = result["prices"]["AAPL"]
    assert price == pytest.approx(230.5)
    assert "query1.finance.yahoo.com" in captured["url"]
    assert captured["params"]["range"] == "5d"


def test_yahoo_history_parses_timestamps_to_dates(monkeypatch):
    def fake_get(url, params=None, headers=None, timeout=None, follow_redirects=False):
        response = MagicMock()
        response.status_code = 200
        response.raise_for_status.return_value = None
        # Two days at known epoch seconds (UTC).
        response.json.return_value = {
            "chart": {
                "result": [
                    {
                        "meta": {"regularMarketPrice": 230.5, "previousClose": 225.0},
                        # 2026-06-15 and 2026-06-16 UTC
                        "timestamp": [1781481600, 1781568000],
                        "indicators": {"quote": [{"close": [225.0, 230.5]}]},
                    }
                ]
            }
        }
        return response

    monkeypatch.setattr("httpx.get", fake_get)

    provider = yahoo.YahooProvider()
    result = provider.fetch_history(
        "1m",
        ["AAPL"],
        {"AAPL": _position("AAPL", "stock", 10, 200.0)},
    )
    assert "AAPL" in result["history"]
    series = result["history"]["AAPL"]
    assert len(series["dates"]) == 2
    assert series["dates"][0] <= series["dates"][1]
    assert series["closes"] == [225.0, 230.5]


def test_yahoo_quote_derives_day_change(monkeypatch):
    def fake_get(url, params=None, headers=None, timeout=None, follow_redirects=False):
        response = MagicMock()
        response.status_code = 200
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "chart": {
                "result": [
                    {
                        "meta": {
                            "regularMarketPrice": 230.0,
                            "chartPreviousClose": 225.0,
                            "currency": "USD",
                            "longName": "Apple Inc.",
                        },
                        "timestamp": [1718000000, 1718086400],
                        "indicators": {
                            "quote": [
                                {
                                    "close": [225.0, 230.0],
                                    "high": [231.0, 234.0],
                                    "low": [220.0, 226.0],
                                    "volume": [50_000_000, 60_000_000],
                                }
                            ]
                        },
                    }
                ]
            }
        }
        return response

    monkeypatch.setattr("httpx.get", fake_get)

    provider = yahoo.YahooProvider()
    quote = provider.quote("AAPL", "stock")
    assert quote is not None
    assert quote["symbol"] == "AAPL"
    assert quote["price"] == pytest.approx(230.0)
    assert quote["day_change"] == pytest.approx(5.0)
    assert quote["day_change_pct"] == pytest.approx(2.2222, rel=1e-3)
    assert quote["year_high"] == pytest.approx(234.0)
    assert quote["year_low"] == pytest.approx(220.0)
    assert quote["currency"] == "USD"
    assert quote["provider"] == "yahoo"


def test_yahoo_handles_network_error_gracefully(monkeypatch):
    def fake_get(url, params=None, headers=None, timeout=None, follow_redirects=False):
        raise RuntimeError("network down")

    monkeypatch.setattr("httpx.get", fake_get)

    provider = yahoo.YahooProvider()
    result = provider.refresh_prices([_position("AAPL", "stock", 10, 200.0)])
    # Errors are surfaced; no crash.
    assert result["prices"] == {}
    assert any("AAPL" in error for error in result["errors"])


# ---------- yahoo retry / host failover ----------

def _ok_chart_response():
    response = MagicMock()
    response.status_code = 200
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "chart": {
            "result": [
                {
                    "meta": {"regularMarketPrice": 101.0, "previousClose": 100.0},
                    "timestamp": [1781481600, 1781568000],
                    "indicators": {"quote": [{"close": [100.0, 101.0]}]},
                }
            ]
        }
    }
    return response


def test_yahoo_429_fails_over_to_query2(monkeypatch):
    """A 429 on query1 retries against the query2 mirror instead of failing."""
    import httpx

    urls = []
    sleeps = []

    def fake_get(url, params=None, headers=None, timeout=None, follow_redirects=False):
        urls.append(url)
        if len(urls) == 1:
            response = MagicMock()
            response.status_code = 429
            request = httpx.Request("GET", url)
            raw = httpx.Response(429, request=request)
            response.raise_for_status.side_effect = httpx.HTTPStatusError(
                "429 Too Many Requests", request=request, response=raw
            )
            return response
        return _ok_chart_response()

    monkeypatch.setattr("httpx.get", fake_get)
    monkeypatch.setattr(yahoo, "_sleep", sleeps.append)

    provider = yahoo.YahooProvider()
    result = provider.refresh_prices([_position("AAPL", "stock", 10, 200.0)])

    assert "AAPL" in result["prices"]
    assert "query1.finance.yahoo.com" in urls[0]
    assert "query2.finance.yahoo.com" in urls[1]
    assert len(sleeps) == 1  # one backoff between attempts


def test_yahoo_non_retryable_error_fails_immediately(monkeypatch):
    """A 404 is not retried — one attempt, clean error."""
    import httpx

    urls = []

    def fake_get(url, params=None, headers=None, timeout=None, follow_redirects=False):
        urls.append(url)
        response = MagicMock()
        response.status_code = 404
        request = httpx.Request("GET", url)
        raw = httpx.Response(404, request=request)
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "404 Not Found", request=request, response=raw
        )
        return response

    monkeypatch.setattr("httpx.get", fake_get)
    monkeypatch.setattr(yahoo, "_sleep", lambda _s: (_ for _ in ()).throw(AssertionError("no sleep on 404")))

    provider = yahoo.YahooProvider()
    result = provider.refresh_prices([_position("NOPE", "stock", 1, 1.0)])

    assert result["prices"] == {}
    assert len(urls) == 1
    assert any("NOPE" in err for err in result["errors"])

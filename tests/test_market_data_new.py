"""Alpha Vantage + Stooq market-data connectors — parsing, filtering, health."""

from __future__ import annotations

from types import SimpleNamespace

from backend.connectors.market_data import alphavantage, cboe, stooq

AV_QUOTE = {
    "Global Quote": {
        "01. symbol": "IBM",
        "03. high": "186.0",
        "04. low": "182.0",
        "05. price": "185.50",
        "06. volume": "1000000",
        "08. previous close": "183.00",
        "09. change": "2.50",
        "10. change percent": "1.37%",
    }
}
AV_DAILY = {
    "Time Series (Daily)": {
        "2026-07-03": {"4. close": "185.50"},
        "2026-07-02": {"4. close": "183.00"},
        "2026-07-01": {"4. close": "181.00"},
    }
}
AV_NOTE = {"Note": "Thank you for using Alpha Vantage! 25 requests per day."}


class FakeResp:
    def __init__(self, *, json=None, text=None):
        self._json = json
        self.text = text or ""

    def json(self):
        return self._json

    def raise_for_status(self):
        return None


def _pos(symbol, asset_type="stock"):
    return SimpleNamespace(symbol=symbol, asset_type=asset_type)


# --- Alpha Vantage ---------------------------------------------------------

def _av_router(monkeypatch, quote=AV_QUOTE, daily=AV_DAILY):
    def fake_get(url, params=None, timeout=None):
        fn = (params or {}).get("function")
        return FakeResp(json=daily if fn == "TIME_SERIES_DAILY" else quote)
    monkeypatch.setattr(alphavantage.httpx, "get", fake_get)


def test_av_refresh_prices(monkeypatch):
    _av_router(monkeypatch)
    out = alphavantage.AlphaVantageConnector({"api_key": "k"}).refresh_prices([_pos("IBM")])
    assert out["prices"]["IBM"] == (185.50, "")


def test_av_history_ascending(monkeypatch):
    _av_router(monkeypatch)
    out = alphavantage.AlphaVantageConnector({"api_key": "k"}).fetch_history("max", ["IBM"], {"IBM": _pos("IBM")})
    hist = out["history"]["IBM"]
    assert hist["dates"] == ["2026-07-01", "2026-07-02", "2026-07-03"]
    assert hist["closes"] == [181.0, 183.0, 185.5]


def test_av_quote_fields(monkeypatch):
    _av_router(monkeypatch)
    q = alphavantage.AlphaVantageConnector({"api_key": "k"}).quote("IBM", "stock")
    assert q["price"] == 185.5 and q["previous_close"] == 183.0
    assert q["day_change_pct"] == 1.37 and q["provider"] == "alphavantage"


def test_av_crypto_is_skipped(monkeypatch):
    _av_router(monkeypatch)
    conn = alphavantage.AlphaVantageConnector({"api_key": "k"})
    assert conn.quote("BTC", "crypto") is None
    out = conn.refresh_prices([_pos("BTC", "crypto")])
    assert "BTC" in out["prices"] or out["errors"]  # skipped, not priced
    assert not out["prices"]


def test_av_rate_limit_surfaced(monkeypatch):
    _av_router(monkeypatch, quote=AV_NOTE)
    conn = alphavantage.AlphaVantageConnector({"api_key": "k"})
    out = conn.refresh_prices([_pos("IBM")])
    assert not out["prices"] and "rate limit" in out["errors"][0].lower()


def test_av_test_requires_key():
    assert alphavantage.AlphaVantageConnector({}).test().ok is False


def test_av_test_ok(monkeypatch):
    _av_router(monkeypatch)
    res = alphavantage.AlphaVantageConnector({"api_key": "k"}).test()
    assert res.ok and "IBM" in res.message


# --- Stooq -----------------------------------------------------------------

STOOQ_CSV = (
    "Date,Open,High,Low,Close,Volume\n"
    "2026-07-01,180,182,179,181.00,1000\n"
    "2026-07-02,181,184,180,183.00,1100\n"
    "2026-07-03,183,186,182,185.50,1200\n"
)


def _stooq_ok(monkeypatch, csv=STOOQ_CSV):
    monkeypatch.setattr(stooq.httpx, "get", lambda url, params=None, timeout=None: FakeResp(text=csv))


def test_stooq_symbol_mapping():
    assert stooq._stooq_symbol("AAPL", "stock") == "aapl.us"
    assert stooq._stooq_symbol("^spx", "stock") == "^spx"
    assert stooq._stooq_symbol("vwrl.uk", "stock") == "vwrl.uk"
    assert stooq._stooq_symbol("BTC", "crypto") is None


def test_stooq_history(monkeypatch):
    _stooq_ok(monkeypatch)
    out = stooq.StooqConnector({}).fetch_history("max", ["AAPL"], {"AAPL": _pos("AAPL")})
    hist = out["history"]["AAPL"]
    assert hist["dates"] == ["2026-07-01", "2026-07-02", "2026-07-03"]
    assert hist["closes"] == [181.0, 183.0, 185.5]


def test_stooq_refresh_last_close(monkeypatch):
    _stooq_ok(monkeypatch)
    out = stooq.StooqConnector({}).refresh_prices([_pos("AAPL")])
    assert out["prices"]["AAPL"] == (185.5, "")


def test_stooq_quote(monkeypatch):
    _stooq_ok(monkeypatch)
    q = stooq.StooqConnector({}).quote("AAPL", "stock")
    assert q["price"] == 185.5 and q["previous_close"] == 183.0
    assert q["year_high"] == 185.5 and q["year_low"] == 181.0 and q["provider"] == "stooq"


def test_stooq_crypto_skipped(monkeypatch):
    _stooq_ok(monkeypatch)
    out = stooq.StooqConnector({}).refresh_prices([_pos("BTC", "crypto")])
    assert not out["prices"] and out["errors"]


def test_stooq_unknown_symbol(monkeypatch):
    monkeypatch.setattr(stooq.httpx, "get", lambda url, params=None, timeout=None: FakeResp(text="<html>error</html>"))
    res = stooq.StooqConnector({}).test()
    assert res.ok is False


# --- Cboe -------------------------------------------------------------------

CBOE_DATA = {
    "data": [
        {"date": "2004-01-02", "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05, "volume": 100},
        {"date": "2026-07-01", "open": 180.0, "high": 182.0, "low": 179.0, "close": 181.0, "volume": 900},
        {"date": "2026-07-02", "open": 181.0, "high": 184.0, "low": 180.5, "close": 183.0, "volume": 800},
        {"date": "2026-07-03", "open": 183.0, "high": 186.0, "low": 182.0, "close": 185.5, "volume": 700},
    ]
}


def _cboe_ok(monkeypatch, payload=CBOE_DATA):
    monkeypatch.setattr(
        cboe.httpx, "get", lambda url, headers=None, timeout=None: FakeResp(json=payload)
    )


def test_cboe_symbol_mapping():
    assert cboe._cboe_symbol("afrm", "stock") == "AFRM"
    assert cboe._cboe_symbol("BTC", "crypto") is None


def test_cboe_history_full_depth_and_period_slice(monkeypatch):
    _cboe_ok(monkeypatch)
    connector = cboe.CboeConnector({})
    # "max" keeps the whole listed lifetime — the point of this source.
    full = connector.fetch_history("max", ["AAPL"], {"AAPL": _pos("AAPL")})
    assert full["history"]["AAPL"]["dates"][0] == "2004-01-02"
    # A bounded period slices locally (one request returns everything). 5y
    # keeps the 2026 fixture rows and drops 2004 for decades to come.
    recent = connector.fetch_history("5y", ["AAPL"], {"AAPL": _pos("AAPL")})
    assert recent["history"]["AAPL"]["dates"] == ["2026-07-01", "2026-07-02", "2026-07-03"]
    assert recent["history"]["AAPL"]["closes"] == [181.0, 183.0, 185.5]


def test_cboe_refresh_and_quote_use_last_close(monkeypatch):
    _cboe_ok(monkeypatch)
    connector = cboe.CboeConnector({})
    assert connector.refresh_prices([_pos("AAPL")])["prices"]["AAPL"] == (185.5, "")
    q = connector.quote("AAPL", "stock")
    assert q["price"] == 185.5
    assert q["previous_close"] == 183.0
    assert q["day_high"] == 186.0
    assert q["provider"] == "cboe"


def test_cboe_failure_is_an_error_not_a_crash(monkeypatch):
    def boom(url, headers=None, timeout=None):
        raise cboe.httpx.ConnectError("no route")

    monkeypatch.setattr(cboe.httpx, "get", boom)
    out = cboe.CboeConnector({}).fetch_history("1y", ["AAPL"], {})
    assert out["history"] == {}
    assert "Cboe request failed" in out["errors"][0]

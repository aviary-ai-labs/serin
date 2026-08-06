"""Fundamentals: chain fallback, cache freshness, provider normalization, endpoint."""

from __future__ import annotations

from backend import connectors, db, fundamentals
from backend.connectors.market_data.alphavantage import AlphaVantageConnector
from backend.main import app
from backend.providers import yahoo as yahoo_provider
from fastapi.testclient import TestClient

AAPL = {
    "symbol": "AAPL",
    "name": "Apple Inc.",
    "kind": "stock",
    "market_cap": 3.0e12,
    "pe_ratio": 30.0,
    "pb_ratio": 45.0,
    "beta": 1.2,
    "dividend_yield": 0.005,
    "expense_ratio": None,
}


class FakeConn:
    """Counts fundamentals calls; returns a canned payload (or None)."""

    def __init__(self, payload=None):
        self.payload = payload
        self.calls = 0

    def fetch_fundamentals(self, symbol, asset_type="stock"):
        self.calls += 1
        return dict(self.payload) if self.payload else None


def _init(tmp_path):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()


def _backdate(symbol: str, days: float) -> None:
    from datetime import UTC, datetime, timedelta

    stamp = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    with db.connect() as conn:
        conn.execute("UPDATE fundamentals SET fetched_at=? WHERE symbol=?", (stamp, symbol))


def test_chain_fallback_and_cache(tmp_path, monkeypatch):
    _init(tmp_path)
    first, second = FakeConn(None), FakeConn(AAPL)
    monkeypatch.setattr(connectors, "market_data_chain", lambda: [("p1", first), ("p2", second)])

    out = fundamentals.get_fundamentals(["AAPL"], {"AAPL": "stock"})
    assert out["AAPL"]["pe_ratio"] == 30.0
    assert out["AAPL"]["source"] == "p2"  # p1 declined, p2 served
    assert (first.calls, second.calls) == (1, 1)

    # Second call is served from cache — no provider traffic.
    out = fundamentals.get_fundamentals(["AAPL"], {"AAPL": "stock"})
    assert out["AAPL"]["source"] == "p2"
    assert (first.calls, second.calls) == (1, 1)

    # Stale (> FRESH_DAYS) → refetched.
    _backdate("AAPL", fundamentals.FRESH_DAYS + 1)
    fundamentals.get_fundamentals(["AAPL"], {"AAPL": "stock"})
    assert (first.calls, second.calls) == (2, 2)


def test_miss_is_negative_cached(tmp_path, monkeypatch):
    _init(tmp_path)
    conn = FakeConn(None)
    monkeypatch.setattr(connectors, "market_data_chain", lambda: [("p1", conn)])

    assert fundamentals.get_fundamentals(["ZZZZ"]) == {}
    assert fundamentals.get_fundamentals(["ZZZZ"]) == {}  # within MISS_FRESH_DAYS
    assert conn.calls == 1

    _backdate("ZZZZ", fundamentals.MISS_FRESH_DAYS + 0.5)
    fundamentals.get_fundamentals(["ZZZZ"])
    assert conn.calls == 2


def test_skip_types_never_fetch(tmp_path, monkeypatch):
    _init(tmp_path)
    conn = FakeConn(AAPL)
    monkeypatch.setattr(connectors, "market_data_chain", lambda: [("p1", conn)])
    out = fundamentals.get_fundamentals(
        ["BTC", "USD-CASH", "MSFT-C430"],
        {"BTC": "crypto", "USD-CASH": "cash", "MSFT-C430": "option"},
    )
    assert out == {}
    assert conn.calls == 0


def test_yahoo_parse_fundamentals():
    summary = {
        "price": {"quoteType": "EQUITY", "longName": "Apple Inc.", "marketCap": {"raw": 3.0e12}},
        "summaryDetail": {
            "trailingPE": {"raw": 30.5},
            "beta": {"raw": 1.25},
            "dividendYield": {"raw": 0.0044},
        },
        "defaultKeyStatistics": {"priceToBook": {"raw": 45.2}},
        "fundProfile": {},
    }
    out = yahoo_provider.parse_fundamentals(summary, "aapl")
    assert out["symbol"] == "AAPL"
    assert out["kind"] == "stock"
    assert out["market_cap"] == 3.0e12
    assert out["pe_ratio"] == 30.5
    assert out["pb_ratio"] == 45.2
    assert out["beta"] == 1.25
    assert out["dividend_yield"] == 0.0044
    assert out["expense_ratio"] is None

    etf = {
        "price": {"quoteType": "ETF", "shortName": "Vanguard S&P 500"},
        "summaryDetail": {"yield": {"raw": 0.013}},
        "fundProfile": {"feesExpensesInvestment": {"annualReportExpenseRatio": {"raw": 0.0003}}},
    }
    out = yahoo_provider.parse_fundamentals(etf, "VOO")
    assert out["kind"] == "etf"
    assert out["expense_ratio"] == 0.0003
    assert out["dividend_yield"] == 0.013
    assert out["market_cap"] is None  # AUM is not holdings' size — stays honest

    assert yahoo_provider.parse_fundamentals({"price": {"quoteType": "EQUITY"}}, "X") is None


def test_fmp_fundamentals(monkeypatch):
    from backend.providers.fmp import FMPProvider

    provider = FMPProvider(api_key="k")
    profile = [{
        "symbol": "AAPL", "companyName": "Apple Inc.", "price": 200.0,
        "marketCap": 3.0e12, "beta": 1.2, "lastDividend": 1.0,
        "isEtf": False, "isFund": False,
    }]
    monkeypatch.setattr(provider, "_fetch", lambda path, params: (profile, None))
    out = provider.fetch_fundamentals("AAPL")
    assert out["kind"] == "stock"
    assert out["market_cap"] == 3.0e12
    assert out["beta"] == 1.2
    assert out["dividend_yield"] == 0.005  # 1.0 / 200
    assert out["pe_ratio"] is None  # not served by the free profile endpoint
    assert out["expense_ratio"] is None

    etf = [{"symbol": "SPCX", "companyName": "SPAC ETF", "price": 30.0,
            "marketCap": 5.0e7, "beta": 1.1, "lastDividend": 0, "isEtf": True}]
    monkeypatch.setattr(provider, "_fetch", lambda path, params: (etf, None))
    out = provider.fetch_fundamentals("SPCX", "etf")
    assert out["kind"] == "etf"
    assert out["market_cap"] is None  # fund AUM never masquerades as cap
    assert out["beta"] == 1.1
    assert out["dividend_yield"] is None  # zero lastDividend → unknown, not 0

    monkeypatch.setattr(provider, "_fetch", lambda path, params: ([], None))
    assert provider.fetch_fundamentals("ZZZZ") is None
    assert provider.fetch_fundamentals("BTC", "crypto") is None


def test_alphavantage_fundamentals(monkeypatch):
    connector = AlphaVantageConnector(config={"api_key": "k"})

    overview = {
        "Symbol": "IBM",
        "Name": "IBM",
        "AssetType": "Common Stock",
        "MarketCapitalization": "150000000000",
        "PERatio": "22.5",
        "PriceToBookRatio": "7.1",
        "Beta": "0.9",
        "DividendYield": "0.037",
    }
    monkeypatch.setattr(connector, "_get", lambda params: (overview, None))
    out = connector.fetch_fundamentals("IBM")
    assert out["kind"] == "stock"
    assert out["market_cap"] == 1.5e11
    assert out["pe_ratio"] == 22.5
    assert out["dividend_yield"] == 0.037

    # ETF: OVERVIEW comes back empty → falls through to ETF_PROFILE.
    def etf_get(params):
        if params.get("function") == "OVERVIEW":
            return {}, None
        return {"net_expense_ratio": "0.0003", "dividend_yield": "0.013"}, None

    monkeypatch.setattr(connector, "_get", etf_get)
    out = connector.fetch_fundamentals("VOO")
    assert out["kind"] == "etf"
    assert out["expense_ratio"] == 0.0003

    # 'None' strings stay None, not 0.0.
    monkeypatch.setattr(
        connector, "_get", lambda params: ({"Symbol": "X", "PERatio": "None", "Beta": "1.1"}, None)
    )
    out = connector.fetch_fundamentals("X")
    assert out["pe_ratio"] is None
    assert out["beta"] == 1.1

    assert AlphaVantageConnector(config={}).fetch_fundamentals("IBM") is None  # no key
    assert connector.fetch_fundamentals("BTC", "crypto") is None


def test_fundamentals_endpoint(tmp_path, monkeypatch):
    _init(tmp_path)
    monkeypatch.setattr(
        "backend.fundamentals.get_fundamentals",
        lambda symbols, asset_types=None, max_age_days=7: {"AAPL": dict(AAPL, source="yahoo")},
    )
    with TestClient(app) as client:
        ok = client.get("/api/quote/AAPL/fundamentals")
        assert ok.status_code == 200
        assert ok.json()["pe_ratio"] == 30.0
        missing = client.get("/api/v1/quote/ZZZZ/fundamentals")
        assert missing.status_code == 404

"""Cboe — the keyless quote provider the chain falls to when FMP's day runs out.

Cboe publishes its own delayed quotes from a CDN: no key, no account, and it
is exchange-published data rather than a page scraped from someone's website.
That makes it the only provider in the chain that costs nothing and asks
nothing, so when the paid tier is exhausted it is what stands between the
dashboard and a stale number.

Two things here are worth more than the rest. Cboe lists a stock called BTC,
and the guard that keeps crypto away from it is the only thing between a
bitcoin holding and being priced at thirty-five dollars. And quotes are
fetched in parallel, so the test that the answers come back attached to the
symbols that asked for them is not ceremony — getting it wrong prices AAPL at
TQQQ's number and nothing on screen would look wrong.
"""

from __future__ import annotations

import httpx
import pytest
from backend.connectors.market_data import cboe
from backend.models import Position


def pos(symbol, asset_type="stock"):
    return Position(id=abs(hash(symbol)) % 9999, symbol=symbol, name=symbol,
                    quantity=1, broker="manual", asset_type=asset_type)


def fake_quotes(prices, monkeypatch, seen=None):
    """Serve quotes/{SYMBOL}.json out of a dict; record what was asked for."""
    def get(url, **kwargs):
        symbol = url.rsplit("/", 1)[-1].removesuffix(".json")
        if seen is not None:
            seen.append(url)
        value = prices.get(symbol)

        class Response:
            status_code = 403 if value is None else 200
            def raise_for_status(self):
                if self.status_code != 200:
                    raise httpx.HTTPStatusError("nope", request=None, response=None)
            def json(self):
                return {"data": {"symbol": symbol, "current_price": value}}

        return Response()

    monkeypatch.setattr(httpx, "get", get)


# --- the BTC trap ----------------------------------------------------------


def test_crypto_is_never_priced_by_cboe(monkeypatch):
    """The one that matters. Cboe answers "BTC" with a listed equity trading
    around $35 — a real number, from a real symbol, that is not bitcoin. A
    holding priced from it is wrong by three orders of magnitude and looks
    entirely plausible on screen. CoinGecko has the coin; this must not
    answer at all."""
    seen = []
    fake_quotes({"BTC": 35.31}, monkeypatch, seen)

    result = cboe.CboeConnector().refresh_prices([pos("BTC", "crypto")])

    assert result["prices"] == {}, "Cboe priced a coin from its equity ticker"
    assert seen == [], "Cboe was asked about a coin at all"
    assert any("crypto" in e.lower() for e in result["errors"])


def test_the_same_ticker_as_a_stock_is_priced_normally(monkeypatch):
    """The guard keys on asset type, not on the letters — a reader who holds
    the equity should still get it."""
    fake_quotes({"BTC": 35.31}, monkeypatch)
    result = cboe.CboeConnector().refresh_prices([pos("BTC", "stock")])
    assert result["prices"]["BTC"][0] == pytest.approx(35.31)


# --- prices land on the right symbols --------------------------------------


def test_parallel_answers_come_back_on_the_symbols_that_asked(monkeypatch):
    """Quotes are fetched concurrently and completion order is arbitrary. If
    the results are zipped back by arrival rather than by request, every price
    is real and attached to the wrong holding — the failure that looks
    correct."""
    book = {f"S{i}": 100.0 + i for i in range(40)}

    def get(url, **kwargs):
        import random, time
        symbol = url.rsplit("/", 1)[-1].removesuffix(".json")
        time.sleep(random.uniform(0, 0.05))       # scramble completion order

        class Response:
            status_code = 200
            def raise_for_status(self): pass
            def json(self): return {"data": {"current_price": book[symbol]}}

        return Response()

    monkeypatch.setattr(httpx, "get", get)
    result = cboe.CboeConnector().refresh_prices([pos(s) for s in book])

    assert result["prices"] == {s: (p, "") for s, p in book.items()}


def test_a_gap_does_not_shift_the_symbols_after_it(monkeypatch):
    """A symbol Cboe will not answer must drop out without sliding every later
    price up one place."""
    fake_quotes({"AAPL": 320.0, "FBALX": None, "TQQQ": 72.0}, monkeypatch)
    result = cboe.CboeConnector().refresh_prices(
        [pos("AAPL"), pos("FBALX"), pos("TQQQ")])

    assert result["prices"]["AAPL"][0] == pytest.approx(320.0)
    assert result["prices"]["TQQQ"][0] == pytest.approx(72.0)
    assert "FBALX" not in result["prices"]


# --- absence is not a price ------------------------------------------------


def test_a_zero_is_absence_not_a_price(monkeypatch):
    """Writing a zero freezes a holding at nothing, which is worse than
    leaving yesterday's number in place."""
    fake_quotes({"AAPL": 0}, monkeypatch)
    result = cboe.CboeConnector().refresh_prices([pos("AAPL")])
    assert result["prices"] == {}
    assert result["errors"]


def test_a_fund_cboe_does_not_list_is_reported_not_invented(monkeypatch):
    """403 is how the CDN says "not a Cboe-listed symbol" — the mutual funds
    in a book come back this way, and no provider in the chain covers them."""
    fake_quotes({"SPAXX": None}, monkeypatch)
    result = cboe.CboeConnector().refresh_prices([pos("SPAXX")])
    assert result["prices"] == {}
    assert any("not listed" in e for e in result["errors"])


def test_one_symbol_failing_does_not_lose_the_rest(monkeypatch):
    def get(url, **kwargs):
        if "TQQQ" in url:
            raise httpx.ConnectError("boom")

        class Response:
            status_code = 200
            def raise_for_status(self): pass
            def json(self): return {"data": {"current_price": 320.0}}

        return Response()

    monkeypatch.setattr(httpx, "get", get)
    result = cboe.CboeConnector().refresh_prices([pos("AAPL"), pos("TQQQ")])
    assert result["prices"]["AAPL"][0] == pytest.approx(320.0)
    assert any("TQQQ" in e for e in result["errors"])


def test_being_throttled_says_so(monkeypatch):
    """Cboe rate-limits, and a sweep that trips it should say that rather than
    report the book as uncoverable — the two call for opposite responses."""
    class Response:
        status_code = 429
        def raise_for_status(self):
            raise httpx.HTTPStatusError("429", request=None, response=None)
        def json(self): return {}

    monkeypatch.setattr(httpx, "get", lambda *a, **k: Response())
    result = cboe.CboeConnector().refresh_prices([pos("AAPL")])
    assert result["prices"] == {}
    assert any("rate limit" in e.lower() for e in result["errors"])


# --- the quote endpoint, not the history one -------------------------------


def test_a_sweep_reads_the_quote_endpoint(monkeypatch):
    """refresh_prices used to read the last row of the historical series,
    which meant downloading a symbol's whole listed lifetime to learn one
    number — and getting a daily close that could be two sessions stale."""
    seen = []
    fake_quotes({"AAPL": 320.0}, monkeypatch, seen)
    cboe.CboeConnector().refresh_prices([pos("AAPL")])

    assert seen == ["https://cdn.cboe.com/api/global/delayed_quotes/quotes/AAPL.json"]
    assert not any("charts/historical" in url for url in seen)


# --- backing off ------------------------------------------------------------


def test_a_throttled_sweep_stops_asking(monkeypatch):
    """Cboe's limiter is a bucket that refills over time, so requests made
    while it is empty keep it empty. A book that carries on asking turns a
    brief throttle into a sustained one — measured at 60 requests a minute,
    37% came back refused, and continuing to ask is what makes that worse.
    The sweep should give up on Cboe for this pass and let the rest of the
    chain answer."""
    attempts = []

    class Response:
        status_code = 429
        def raise_for_status(self): pass
        def json(self): return {}

    def get(url, **kwargs):
        attempts.append(url)
        return Response()

    monkeypatch.setattr(httpx, "get", get)
    book = [pos(f"S{i}") for i in range(200)]
    result = cboe.CboeConnector().refresh_prices(book)

    assert result["prices"] == {}
    assert len(attempts) < 50, (
        f"kept asking a throttling provider {len(attempts)} times for 200 symbols"
    )
    # Every symbol still gets an answer, so the reader is told the book was
    # rate-limited rather than that it does not exist.
    assert len(result["errors"]) == 200
    assert all("rate limit" in e.lower() for e in result["errors"])


def test_an_occasional_refusal_does_not_abandon_the_sweep(monkeypatch):
    """A scattered 429 among good answers is normal and must not stop the
    pass — only a solid wall of them means the bucket is empty."""
    def get(url, **kwargs):
        symbol = url.rsplit("/", 1)[-1].removesuffix(".json")
        throttled = int(symbol[1:]) % 4 == 0

        class Response:
            status_code = 429 if throttled else 200
            def raise_for_status(self): pass
            def json(self): return {"data": {"current_price": 100.0}}

        return Response()

    monkeypatch.setattr(httpx, "get", get)
    result = cboe.CboeConnector().refresh_prices([pos(f"S{i}") for i in range(80)])
    assert len(result["prices"]) == 60, "gave up while most of the book was answering"


def test_something_in_the_tree_actually_prices_a_coin():
    """The other half of the BTC trap. Cboe refusing crypto is only safe if
    something else covers it — otherwise a bitcoin holding is refused by every
    provider in turn and priced by none, which is what a deployment did for
    weeks: FMP rate-limited, Cboe declined, Yahoo rate-limited, and the crypto
    layer that the error message named was switched off.

    CoinGecko needs no key, so there is no reason for it to be opt-in."""
    from backend.connectors.market_data.coingecko import CoinGeckoConnector

    assert CoinGeckoConnector.manifest.default_enabled, (
        "the only crypto-capable provider is off by default — coins price nowhere"
    )
    assert CoinGeckoConnector.manifest.asset_scope == "crypto"
    assert not any(f.required for f in CoinGeckoConnector.manifest.config_schema), (
        "a default-on connector must not require configuration"
    )

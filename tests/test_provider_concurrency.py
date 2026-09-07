"""Symbol fetches run concurrently, and stay correct while doing it.

Connecting a second brokerage added eleven new symbols, and the dashboard took
twenty seconds to draw. The cause was not SnapTrade and not the database: the
Yahoo provider walked the batch one HTTP round trip at a time, so a cold cache
cost one network latency per holding, serially. Production logs showed
`GET /api/price-history -> 200 (19491.9ms)` three times in a row.

Concurrency is easy to add and easy to get subtly wrong, so these pin the three
properties that matter: it really is concurrent, results still line up with the
symbols they belong to, and one bad symbol cannot take the batch down.
"""

from __future__ import annotations

import threading
import time

import pytest
from backend.models import Position
from backend.providers import yahoo

SYMBOLS = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
DELAY = 0.15


def position(symbol, asset_type="stock"):
    return Position(id=1, symbol=symbol, name=symbol, quantity=1, cost_basis=0.0,
                    price=1.0, broker="x", asset_type=asset_type, currency="USD")


def chart_for(symbol):
    """A minimal well-formed chart payload, with a close encoding the symbol so
    a mis-zipped result is detectable rather than merely wrong."""
    seed = float(sum(ord(c) for c in symbol))
    return {
        "timestamp": [1767225600, 1767312000],
        "indicators": {"quote": [{"close": [seed, seed + 1]}]},
        "meta": {"regularMarketPrice": seed},
    }


@pytest.fixture
def slow_chart(monkeypatch):
    """Every call sleeps, and records how many were in flight at once."""
    state = {"peak": 0, "live": 0, "calls": []}
    lock = threading.Lock()

    def fake(symbol, *args, **kwargs):
        with lock:
            state["live"] += 1
            state["peak"] = max(state["peak"], state["live"])
            state["calls"].append(symbol)
        time.sleep(DELAY)
        with lock:
            state["live"] -= 1
        return chart_for(symbol), None

    monkeypatch.setattr(yahoo, "_chart", fake)
    return state


def test_history_for_many_symbols_is_not_one_round_trip_at_a_time(slow_chart, monkeypatch):
    monkeypatch.setattr(yahoo, "_MAX_PARALLEL", 6)
    by_symbol = {s: position(s) for s in SYMBOLS}
    start = time.perf_counter()
    yahoo.provider().fetch_history("3m", SYMBOLS, by_symbol)
    elapsed = time.perf_counter() - start
    serial = DELAY * len(SYMBOLS)
    assert elapsed < serial / 2, (
        f"{elapsed:.2f}s for {len(SYMBOLS)} symbols against a {serial:.2f}s "
        "serial baseline — the fetches are still sequential"
    )
    assert slow_chart["peak"] > 1, "no two requests were ever in flight together"


def test_quotes_are_fetched_concurrently_too(slow_chart, monkeypatch):
    """The same loop shape sits in refresh_prices, and a sync repriced eleven
    new symbols through it — part of the twenty-two-second sync."""
    # This test measures the per-symbol chart path. refresh_prices now
    # tries a batched quote first, which would bootstrap a crumb over the
    # network before the code under test ever runs.
    monkeypatch.setattr(yahoo, "_batch_quotes", lambda _by_symbol: {})
    monkeypatch.setattr(yahoo, "_MAX_PARALLEL", 6)
    start = time.perf_counter()
    yahoo.provider().refresh_prices([position(s) for s in SYMBOLS])
    elapsed = time.perf_counter() - start
    assert elapsed < DELAY * len(SYMBOLS) / 2
    assert slow_chart["peak"] > 1


def test_concurrency_is_bounded_rather_than_unlimited(slow_chart, monkeypatch):
    """Yahoo answers 429 under load. Firing twenty simultaneous requests would
    trade a slow dashboard for a rate-limited one."""
    monkeypatch.setattr(yahoo, "_MAX_PARALLEL", 3)
    many = [f"S{i:02d}" for i in range(20)]
    yahoo.provider().fetch_history("3m", many, {s: position(s) for s in many})
    assert slow_chart["peak"] <= 3, f"{slow_chart['peak']} requests were in flight at once"


def test_each_series_lands_against_its_own_symbol(slow_chart, monkeypatch):
    """The failure concurrency invites: results returned out of order and
    zipped back onto the wrong tickers, so every holding shows another
    holding's prices. Plausible on screen, and completely wrong."""
    monkeypatch.setattr(yahoo, "_MAX_PARALLEL", 6)
    by_symbol = {s: position(s) for s in SYMBOLS}
    out = yahoo.provider().fetch_history("3m", SYMBOLS, by_symbol)
    assert set(out["history"]) == set(SYMBOLS)
    for symbol, series in out["history"].items():
        expected = float(sum(ord(c) for c in symbol))
        assert series["closes"][0] == expected, f"{symbol} received another symbol's series"


def test_one_failing_symbol_does_not_lose_the_others(monkeypatch):
    monkeypatch.setattr(yahoo, "_MAX_PARALLEL", 6)

    def fake(symbol, *args, **kwargs):
        if symbol == "CCC":
            return None, "Yahoo is rate-limiting price requests (HTTP 429)."
        return chart_for(symbol), None

    monkeypatch.setattr(yahoo, "_chart", fake)
    out = yahoo.provider().fetch_history("3m", SYMBOLS, {s: position(s) for s in SYMBOLS})
    assert set(out["history"]) == set(SYMBOLS) - {"CCC"}
    assert any("CCC" in e for e in out["errors"])


def test_a_single_symbol_takes_no_thread_pool_at_all(slow_chart, monkeypatch):
    """The common case — one position added, or a single-symbol chart. Paying
    for a pool there is pure overhead."""
    monkeypatch.setattr(yahoo, "_MAX_PARALLEL", 6)
    out = yahoo.provider().fetch_history("3m", ["AAA"], {"AAA": position("AAA")})
    assert slow_chart["peak"] == 1
    assert set(out["history"]) == {"AAA"}


def test_an_empty_batch_does_nothing(monkeypatch):
    monkeypatch.setattr(yahoo, "_chart", lambda *a, **k: pytest.fail("should not fetch"))
    assert yahoo.provider().fetch_history("3m", [], {})["history"] == {}

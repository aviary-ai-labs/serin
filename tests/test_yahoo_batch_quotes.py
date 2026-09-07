"""Batched quotes, and the fallback that keeps a Yahoo change cheap.

The chart endpoint is one HTTP request per symbol, so a 31-holding deployment
spent 31 requests on every sweep. That is what made a one-minute cadence cost
about 12,000 requests a day and kept the sweep at fifteen minutes — a
staleness a reader notices with their broker open in the next tab.

Batched, a once-a-minute sweep costs fewer requests per day than the
fifteen-minute one did. The tests below care about two things: that the batch
is actually used, and that its failure is never worse than not having it.
"""

from __future__ import annotations

import pytest
from backend.models import Position
from backend.providers import yahoo


def pos(symbol, asset_type="stock"):
    return Position(
        id=abs(hash(symbol)) % 9999, symbol=symbol, name=symbol, quantity=1,
        cost_basis=0.0, price=0.0, broker="robinhood", asset_type=asset_type,
        currency="USD",
    )


@pytest.fixture
def calls(monkeypatch):
    """Count what actually leaves the process."""
    seen = {"batch": 0, "chart": []}
    monkeypatch.setattr(yahoo, "_get_crumb", lambda force=False: ({}, "crumb"))

    def chart(symbol, *args, **kwargs):
        seen["chart"].append(symbol)
        return {"meta": {"regularMarketPrice": 1.0}}, None

    monkeypatch.setattr(yahoo, "_chart", chart)
    return seen


def install_batch(monkeypatch, calls, payload, status=200):
    import httpx

    class Response:
        status_code = status
        def raise_for_status(self):
            if status >= 400:
                raise httpx.HTTPError("boom")
        def json(self):
            return payload

    def get(url, **kwargs):
        if "/v7/finance/quote" in url:
            calls["batch"] += 1
            return Response()
        raise AssertionError(f"unexpected request: {url}")

    monkeypatch.setattr(httpx, "get", get)


def test_a_whole_book_costs_one_request(monkeypatch, calls):
    """The point of the change. Thirty-one symbols were thirty-one requests."""
    symbols = [f"SYM{i}" for i in range(31)]
    install_batch(monkeypatch, calls, {"quoteResponse": {"result": [
        {"symbol": s, "regularMarketPrice": 10.0 + i} for i, s in enumerate(symbols)
    ]}})
    result = yahoo.YahooProvider().refresh_prices([pos(s) for s in symbols])
    assert calls["batch"] == 1
    assert calls["chart"] == [], "the per-symbol path ran anyway"
    assert len(result["prices"]) == 31
    assert result["prices"]["SYM0"][0] == 10.0


def test_a_symbol_the_batch_skips_falls_back_to_its_own_request(monkeypatch, calls):
    """Best-effort by design: a gap costs latency, never a price."""
    install_batch(monkeypatch, calls, {"quoteResponse": {"result": [
        {"symbol": "AAA", "regularMarketPrice": 10.0},
    ]}})
    result = yahoo.YahooProvider().refresh_prices([pos("AAA"), pos("BBB")])
    assert calls["batch"] == 1
    assert calls["chart"] == ["BBB"]
    assert set(result["prices"]) == {"AAA", "BBB"}


def test_a_broken_batch_endpoint_prices_everything_anyway(monkeypatch, calls):
    """Yahoo has broken this auth before. When it does, the sweep gets slower
    and not wrong."""
    install_batch(monkeypatch, calls, {}, status=500)
    result = yahoo.YahooProvider().refresh_prices([pos("AAA"), pos("BBB")])
    assert sorted(calls["chart"]) == ["AAA", "BBB"]
    assert set(result["prices"]) == {"AAA", "BBB"}


def test_a_missing_crumb_does_not_stall_the_sweep(monkeypatch, calls):
    monkeypatch.setattr(yahoo, "_get_crumb", lambda force=False: (None, None))
    result = yahoo.YahooProvider().refresh_prices([pos("AAA")])
    assert calls["chart"] == ["AAA"]
    assert result["prices"]["AAA"][0] == 1.0


def test_a_quote_with_no_usable_price_is_left_to_the_fallback(monkeypatch, calls):
    """A zero is not a price. Taking it would freeze a holding at nothing."""
    install_batch(monkeypatch, calls, {"quoteResponse": {"result": [
        {"symbol": "AAA", "regularMarketPrice": 0},
    ]}})
    result = yahoo.YahooProvider().refresh_prices([pos("AAA")])
    assert calls["chart"] == ["AAA"]
    assert result["prices"]["AAA"][0] == 1.0


def test_previous_close_covers_a_symbol_between_sessions(monkeypatch, calls):
    install_batch(monkeypatch, calls, {"quoteResponse": {"result": [
        {"symbol": "AAA", "regularMarketPrice": None, "previousClose": 42.5},
    ]}})
    result = yahoo.YahooProvider().refresh_prices([pos("AAA")])
    assert result["prices"]["AAA"][0] == 42.5
    assert calls["chart"] == []


def test_more_than_one_batch_when_the_book_is_large(monkeypatch, calls):
    symbols = [f"S{i}" for i in range(120)]
    install_batch(monkeypatch, calls, {"quoteResponse": {"result": []}})
    yahoo.YahooProvider().refresh_prices([pos(s) for s in symbols])
    assert calls["batch"] == 3, "120 symbols should be three batches of 50"


# --- the cadence is arithmetic, and it has to survive the book growing -----


def _chain(monkeypatch, *budgets):
    from backend import prices

    conns = [(f"p{i}", type("C", (), {"quote_budget": b})()) for i, b in enumerate(budgets)]
    monkeypatch.setattr(prices.connectors, "market_data_chain", lambda: conns)
    return prices


def test_a_small_book_on_a_per_minute_plan_refreshes_every_minute(monkeypatch):
    """21 symbols, one request each, against 60 a minute: a minute fits."""
    from backend.connectors.base import QuoteBudget

    prices = _chain(monkeypatch, QuoteBudget(batch_size=1, per_minute=60))
    assert prices.sweep_interval_seconds(21) == prices.SWEEP_SECONDS_MIN


def test_the_same_plan_slows_down_rather_than_breaking_at_500(monkeypatch):
    """The reason this is arithmetic and not a flag. 500 symbols at one
    request each needs eight minutes of a 60-a-minute allowance, so the sweep
    lands on a longer interval instead of exhausting the plan."""
    from backend.connectors.base import QuoteBudget

    prices = _chain(monkeypatch, QuoteBudget(batch_size=1, per_minute=60))
    interval = prices.sweep_interval_seconds(500)
    assert interval == pytest.approx(500, abs=60), interval
    assert interval > prices.SWEEP_SECONDS_MIN


def test_batching_is_what_carries_a_500_symbol_book(monkeypatch):
    """The same 500 symbols on a provider that takes 100 per request is five
    requests a sweep, and a minute fits again. Batch size, not plan size, is
    the lever that scales."""
    from backend.connectors.base import QuoteBudget

    prices = _chain(monkeypatch, QuoteBudget(batch_size=100, per_minute=60))
    assert prices.sweep_interval_seconds(500) == prices.SWEEP_SECONDS_MIN


def test_a_daily_allowance_is_spread_across_the_session(monkeypatch):
    """FMP's free tier: 250 a day, 50 symbols a request. 21 symbols is one
    request a sweep, and only part of the allowance belongs to quotes."""
    from backend.connectors.base import QuoteBudget

    prices = _chain(monkeypatch, QuoteBudget(batch_size=50, per_day=250))
    interval = prices.sweep_interval_seconds(21)
    sweeps = (7 * 60 * 60) / interval
    assert sweeps <= 250 * 0.6, f"{sweeps:.0f} sweeps/day overspends a 250/day tier"
    assert interval >= prices.SWEEP_SECONDS_MIN


def test_the_best_provider_in_the_chain_sets_the_pace(monkeypatch):
    """The chain stops as soon as one provider has answered, so the cadence is
    what the best of them can sustain — not the worst."""
    from backend.connectors.base import QuoteBudget

    prices = _chain(monkeypatch,
                    QuoteBudget(batch_size=50, per_day=250),
                    QuoteBudget(batch_size=100, per_minute=60))
    assert prices.sweep_interval_seconds(500) == prices.SWEEP_SECONDS_MIN


def test_a_provider_that_declares_nothing_does_not_speed_anything_up(monkeypatch):
    """Silence is not permission: an undeclared budget must not be read as an
    unlimited one."""
    from backend.connectors.base import QuoteBudget

    prices = _chain(monkeypatch, QuoteBudget())
    assert prices.sweep_interval_seconds(500) == prices.SWEEP_SECONDS_DEFAULT


def test_an_unreadable_registry_does_not_stall_the_sweep(monkeypatch):
    from backend import prices

    def boom():
        raise RuntimeError("registry down")

    monkeypatch.setattr(prices.connectors, "market_data_chain", boom)
    assert prices.sweep_interval_seconds(21) == prices.SWEEP_SECONDS_DEFAULT


def test_the_interval_is_never_absurd_in_either_direction(monkeypatch):
    """A vast plan must not poll faster than anyone can read, and a tiny one
    must not produce a cadence that looks broken."""
    from backend.connectors.base import QuoteBudget

    prices = _chain(monkeypatch, QuoteBudget(batch_size=1000, per_minute=100_000))
    assert prices.sweep_interval_seconds(500) == prices.SWEEP_SECONDS_MIN

    prices = _chain(monkeypatch, QuoteBudget(batch_size=1, per_day=25))
    assert prices.sweep_interval_seconds(500) == prices.SWEEP_SECONDS_MAX

"""Batched quotes for the provider this deployment actually uses.

refresh_prices asked per symbol, and asked `stable/profile` first because it
carries the sector — so a 21-holding sweep spent 21 requests to learn 21
numbers, four times an hour, and both providers were seen returning 429.

Sector is a fact about a company that does not change between sweeps. The
price is the only thing worth re-asking for, and the quote endpoint takes a
list.
"""

from __future__ import annotations

import pytest
from backend.models import Position
from backend.providers import fmp


def pos(symbol, sector="", asset_type="stock"):
    return Position(
        id=abs(hash(symbol)) % 9999, symbol=symbol, name=symbol, quantity=1,
        cost_basis=0.0, price=0.0, broker="robinhood", asset_type=asset_type,
        currency="USD", sector=sector,
    )


@pytest.fixture
def provider(monkeypatch):
    """A provider whose every outbound request is recorded."""
    p = fmp.FMPProvider(api_key="test-key") if hasattr(fmp, "FMPProvider") else fmp.provider()
    calls = []

    def fetch(path, params=None):
        calls.append((path, dict(params or {})))
        if path == "stable/quote":
            # A one-symbol batch is still a batch; the real endpoint does not
            # care how long the list is.
            wanted = (params or {}).get("symbol", "")
            return [{"symbol": s, "price": 100.0 + i}
                    for i, s in enumerate(wanted.split(",")) if s], None
        if path == "stable/profile":
            return [{"price": 42.0, "sector": "Technology"}], None
        return [], None

    monkeypatch.setattr(p, "_fetch", fetch)
    p.calls = calls
    return p


def test_a_book_with_known_sectors_costs_one_request(provider):
    """The point of the change: 21 requests became one."""
    positions = [pos(f"S{i}", sector="Technology") for i in range(21)]
    result = provider.refresh_prices(positions)

    assert len(provider.calls) == 1, [c[0] for c in provider.calls]
    assert provider.calls[0][0] == "stable/quote"
    assert len(result["prices"]) == 21
    assert result["prices"]["S0"][0] == 100.0
    assert result["prices"]["S0"][1] == "Technology"


def test_a_symbol_with_no_sector_yet_still_fetches_its_profile(provider):
    """Sector has to come from somewhere the first time. After that the
    position carries it and the sweep stops asking."""
    result = provider.refresh_prices([pos("AAA", sector=""), pos("BBB", sector="Tech")])
    paths = [c[0] for c in provider.calls]
    assert paths.count("stable/quote") == 1
    assert paths.count("stable/profile") == 1, paths
    assert result["prices"]["AAA"][1] == "Technology"
    assert result["prices"]["BBB"][1] == "Tech"


def test_the_batched_price_wins_over_the_profiles(provider):
    """The profile carries a price too, and it is the staler of the two."""
    result = provider.refresh_prices([pos("AAA", sector="")])
    assert result["prices"]["AAA"][0] == 100.0, "the profile's 42.0 was used"


def test_a_failing_batch_leaves_every_symbol_priced(provider, monkeypatch):
    """Best-effort: an endpoint change costs requests, never prices."""
    def fetch(path, params=None):
        provider.calls.append((path, dict(params or {})))
        if path == "stable/quote":
            return None, "429 Too Many Requests"
        return [{"price": 42.0, "sector": "Technology"}], None

    monkeypatch.setattr(provider, "_fetch", fetch)
    result = provider.refresh_prices([pos("AAA"), pos("BBB")])
    assert set(result["prices"]) == {"AAA", "BBB"}
    assert result["prices"]["AAA"][0] == 42.0


def test_a_large_book_splits_into_batches(provider):
    provider.refresh_prices([pos(f"S{i}", sector="Tech") for i in range(120)])
    quotes = [c for c in provider.calls if c[0] == "stable/quote"]
    assert len(quotes) == 3, "120 symbols should be three batches of 50"


def test_a_coin_does_not_cost_a_profile_request(provider):
    """No provider reports a sector for a coin, so falling through to the
    profile call spent a request every sweep to learn nothing. On a 250-a-day
    budget that is half the sweep's cost."""
    result = provider.refresh_prices([
        pos("AAPL", sector="Technology"),
        pos("BTC", sector="", asset_type="crypto"),
    ])
    paths = [c[0] for c in provider.calls]
    assert paths == ["stable/quote"], paths
    assert set(result["prices"]) == {"AAPL", "BTC"}
    assert result["prices"]["BTC"][1] == "", "a sector was invented for a coin"


def test_an_equity_without_a_sector_still_fetches_one(provider):
    """The saving is specific to asset classes that have no sector at all."""
    provider.refresh_prices([pos("AAA", sector="")])
    assert "stable/profile" in [c[0] for c in provider.calls]

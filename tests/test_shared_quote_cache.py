"""The shared quote cache — one fetch per symbol, not per holder.

Quotes used to be fetched per user and written onto that user's position rows,
so a hundred customers holding AAPL bought the same number a hundred times.
These tests pin the property that made the change worth making: the provider
bill tracks distinct symbols, not the people holding them.
"""

from __future__ import annotations

from datetime import UTC

import pytest
from backend import db, prices, scope
from backend.config import settings
from backend.models import PositionIn
from backend.providers import fmp as fmp_provider

A = "user-a"
B = "user-b"


@pytest.fixture
def fresh_db(tmp_path):
    db.set_db_path(tmp_path / "quotes.db")
    db.init_db()
    yield


def _no_ambient_user() -> str:
    raise RuntimeError("no authenticated user in this request context")


@pytest.fixture
def multiuser():
    scope.set_scope_provider(_no_ambient_user)
    yield
    scope.set_scope_provider(None)


@pytest.fixture
def fmp(monkeypatch):
    """A counting FMP stand-in — the call count is the thing under test."""
    monkeypatch.setattr(settings, "market_data_provider", "fmp")
    monkeypatch.setattr(settings, "fmp_api_key", "test-key")
    calls: list[str] = []

    def fake_get(path, params, *args, **kwargs):
        if path == "stable/profile":
            calls.append(params["symbol"])
            return [{"price": 212.5, "sector": "Technology"}], None
        raise AssertionError(path)

    monkeypatch.setattr(fmp_provider, "_get", fake_get)
    return calls


def _hold(symbol: str, asset_type: str = "stock") -> None:
    db.create_position(
        PositionIn(
            symbol=symbol,
            broker="manual",
            asset_type=asset_type,
            quantity=1,
            average_cost=100,
            current_price=0,
        )
    )


def test_two_users_holding_one_symbol_cost_one_fetch(fresh_db, multiuser, fmp):
    with scope.using(A):
        _hold("AAPL")
        prices.refresh_prices()
    with scope.using(B):
        _hold("AAPL")
        result = prices.refresh_prices()

    assert fmp == ["AAPL"]  # not ["AAPL", "AAPL"]
    assert result["cached"] == 1  # B was served from what A's fetch paid for
    with scope.using(B):
        assert db.list_positions()[0].current_price == 212.5  # ...and still priced


def test_a_stale_cache_entry_goes_back_to_the_provider(fresh_db, fmp):
    _hold("AAPL")
    prices.refresh_prices()
    assert fmp == ["AAPL"]
    # max_age_seconds=0 is what the deployment-wide sweep passes: never cached.
    prices.refresh_prices(max_age_seconds=0)
    assert fmp == ["AAPL", "AAPL"]


def test_positions_declare_themselves_to_the_work_list(fresh_db):
    _hold("AAPL")
    _hold("BTC", asset_type="crypto")
    _hold("USD", asset_type="cash")  # nothing to quote
    tracked = db.list_tracked_symbols()
    assert ("AAPL", "stock") in tracked
    assert ("BTC", "crypto") in tracked
    assert not [pair for pair in tracked if pair[0] == "USD"]


def test_sweep_prices_the_whole_deployment_without_reading_positions(fresh_db, multiuser, fmp):
    """The sweep works off symbols alone — it never asks who holds what, which
    is what lets it run at instance scope under Postgres RLS."""
    with scope.using(A):
        _hold("AAPL")
    with scope.using(B):
        _hold("MSFT")

    # Every DB read resolves a scope even for un-scoped tables, so the sweep
    # runs at instance scope — as the scheduler wraps it.
    with scope.using(scope.INSTANCE_SCOPE):
        result = prices.refresh_tracked_quotes()
        cached = db.get_cached_quotes()

    assert result["symbols"] == 2
    assert sorted(fmp) == ["AAPL", "MSFT"]
    assert cached[("AAPL", "stock")][0] == 212.5
    assert cached[("MSFT", "stock")][0] == 212.5

    # And now neither user's refresh touches the provider.
    fmp.clear()
    for owner in (A, B):
        with scope.using(owner):
            assert prices.refresh_prices()["cached"] == 1
    assert fmp == []


def test_cache_survives_a_provider_outage(fresh_db, fmp, monkeypatch):
    """A cached price is better than no price: if the provider dies after the
    cache is warm, the window still answers."""
    _hold("AAPL")
    prices.refresh_prices()

    def dead(path, params, *args, **kwargs):
        raise AssertionError("provider must not be called while the cache is fresh")

    monkeypatch.setattr(fmp_provider, "_get", dead)
    result = prices.refresh_prices()
    assert result["cached"] == 1
    assert result["errors"] == []


def test_an_existing_database_gains_the_cache_on_upgrade(tmp_path):
    """Migration 7, from the perspective of a self-hoster who already has
    holdings: the tables appear and the work-list arrives pre-seeded."""
    import sqlite3

    from backend import db as db_module

    path = tmp_path / "legacy.db"
    db.set_db_path(path)
    db.init_db()
    _hold("AAPL")
    _hold("USD", asset_type="cash")

    # Rewind to a pre-cache database: drop the new tables and the version row.
    with sqlite3.connect(path) as raw:
        raw.execute("DROP TABLE tracked_symbols")
        raw.execute("DROP TABLE quotes")
        raw.execute("DELETE FROM schema_version WHERE version=7")
        raw.commit()

    db_module.init_db()

    tracked = db.list_tracked_symbols()
    assert ("AAPL", "stock") in tracked          # seeded from existing holdings
    assert not [pair for pair in tracked if pair[0] == "USD"]
    assert db.get_cached_quotes() == {}          # empty, but present


def test_missing_tables_degrade_to_the_old_behaviour(fresh_db, fmp, caplog):
    """Postgres upgrades code before an operator runs the DDL step — the app
    role has no CREATE rights, by design. Until then the cache must cost
    speed, not function."""
    import sqlite3

    _hold("AAPL")
    with sqlite3.connect(db.DB_PATH) as raw:
        raw.execute("DROP TABLE quotes")
        raw.execute("DROP TABLE tracked_symbols")
        raw.commit()

    # Positions still save — the tracking write must not roll back a holding.
    _hold("MSFT")
    assert {p.symbol for p in db.list_positions()} == {"AAPL", "MSFT"}

    # And pricing still works, by going to the provider every time.
    result = prices.refresh_prices()
    assert result["cached"] == 0
    assert result["updated"] == 2
    assert sorted(fmp) == ["AAPL", "MSFT"]
    assert db.list_tracked_symbols() == []


# ---------------------------------------------------------------------------
# Free-tier budget: sweeps must not spend calls that cannot buy new data


def test_market_hours_check_reads_the_clock_correctly():
    from datetime import datetime

    # Tuesday 2026-08-11, 14:00 UTC = 10:00 ET — trading.
    assert prices._us_equity_market_open(datetime(2026, 8, 11, 14, 0, tzinfo=UTC))
    # Tuesday 03:00 UTC = Monday 23:00 ET — closed.
    assert not prices._us_equity_market_open(datetime(2026, 8, 11, 3, 0, tzinfo=UTC))
    # Saturday noon ET — closed.
    assert not prices._us_equity_market_open(datetime(2026, 8, 15, 16, 0, tzinfo=UTC))


def test_closed_market_sweep_skips_priced_stocks_but_not_new_ones(fresh_db, fmp, monkeypatch):
    _hold("AAPL")
    with scope.using(scope.LOCAL_SCOPE):
        prices.refresh_tracked_quotes()  # warm AAPL while "open"
    fmp.clear()

    monkeypatch.setattr(prices, "_us_equity_market_open", lambda now=None: False)
    _hold("MSFT")  # added after hours, never priced
    with scope.using(scope.LOCAL_SCOPE):
        result = prices.refresh_tracked_quotes()

    assert fmp == ["MSFT"]  # AAPL's Friday close cannot change; MSFT deserves a first price
    assert result["skipped"] == 1
    assert result["symbols"] == 1


def test_history_sweep_tops_up_and_then_stands_down(fresh_db, monkeypatch):
    from datetime import UTC, datetime, timedelta

    monkeypatch.setattr(settings, "market_data_provider", "fmp")
    monkeypatch.setattr(settings, "fmp_api_key", "test-key")
    history_calls: list[tuple[str, str]] = []
    today = datetime.now(UTC).date()

    def fake_get(path, params, *args, **kwargs):
        if path == "stable/profile":
            return [{"price": 212.5, "sector": "Technology"}], None
        if path == "stable/historical-price-eod/light":
            history_calls.append((params["symbol"], params.get("from", "")))
            days = [today - timedelta(days=n) for n in range(3, -1, -1)]
            return [{"date": d.isoformat(), "close": 100.0 + n} for n, d in enumerate(days)], None
        raise AssertionError(path)

    monkeypatch.setattr(fmp_provider, "_get", fake_get)
    _hold("AAPL")

    with scope.using(scope.LOCAL_SCOPE):
        first = prices.refresh_tracked_history()
        again = prices.refresh_tracked_history()

    assert [sym for sym, _ in history_calls] == ["AAPL"]  # one call, once
    # ...and that one bootstrap call asks for full depth, not a year — the
    # provider charges the same either way.
    bootstrap_from = history_calls[0][1]
    assert bootstrap_from <= (today - timedelta(days=365 * 5)).isoformat()
    assert first["symbols"] == 1
    assert again["skipped"] == 1              # today's close is already cached
    cached = db.get_cached_price_history(["AAPL"])["AAPL"]
    assert cached["dates"][-1] == today.isoformat()

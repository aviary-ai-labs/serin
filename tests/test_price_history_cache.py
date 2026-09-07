from __future__ import annotations

from datetime import UTC, datetime, timedelta

from backend import db, prices
from backend.config import settings
from backend.connectors.market_data import cboe as cboe_connector
from backend.models import PositionIn
from backend.providers import fmp as fmp_provider
from backend.providers import yahoo as yahoo_provider


def _mute_yahoo(monkeypatch):
    """Down the keyless chain members (Yahoo, Cboe) so 'provider down' tests
    exercise the cache path deterministically (no real network)."""
    monkeypatch.setattr(yahoo_provider, "_get", lambda *a, **k: (None, "429 Too Many Requests"))
    monkeypatch.setattr(
        cboe_connector, "_fetch_series", lambda *a, **k: ([], "Cboe request failed: 429")
    )


def _days_ago(n: int) -> str:
    return (datetime.now(UTC) - timedelta(days=n)).date().isoformat()


def _use_fmp(tmp_path, monkeypatch):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()
    db.create_position(
        PositionIn(
            symbol="AAPL", broker="manual", asset_type="stock",
            quantity=2, average_cost=100, current_price=200,
        )
    )
    monkeypatch.setattr(settings, "market_data_provider", "fmp")
    monkeypatch.setattr(settings, "fmp_api_key", "test-key")


def test_fresh_history_is_written_to_cache(tmp_path, monkeypatch):
    _use_fmp(tmp_path, monkeypatch)
    d1, d2 = _days_ago(2), _days_ago(1)

    def fake_get(path, params, *args, **kwargs):
        return [
            {"date": d2, "price": 211.5},
            {"date": d1, "price": 209.0},
        ], None

    monkeypatch.setattr(fmp_provider, "_get", fake_get)

    result = prices.fetch_price_history("1w")

    assert result["history"]["AAPL"]["dates"] == [d1, d2]
    assert result["history"]["AAPL"]["closes"] == [209.0, 211.5]
    assert result["cached"] == []  # nothing served from cache on a fresh hit

    cached = db.get_cached_price_history(["AAPL"])
    assert cached["AAPL"]["dates"] == [d1, d2]
    assert cached["AAPL"]["closes"] == [209.0, 211.5]


def test_rate_limited_history_falls_back_to_cache(tmp_path, monkeypatch):
    _use_fmp(tmp_path, monkeypatch)
    d1, d2 = _days_ago(2), _days_ago(1)

    # 1) Prime the cache with a successful fetch.
    def ok_get(path, params, *args, **kwargs):
        return [{"date": d2, "price": 211.5}, {"date": d1, "price": 209.0}], None

    monkeypatch.setattr(fmp_provider, "_get", ok_get)
    prices.fetch_price_history("1w")

    # 2) Provider is now rate-limited (429): returns an error and no rows.
    #    refresh=True forces the provider pass despite the fresh cache.
    def rate_limited_get(path, params, *args, **kwargs):
        return None, "FMP request failed: 429 Too Many Requests"

    monkeypatch.setattr(fmp_provider, "_get", rate_limited_get)
    _mute_yahoo(monkeypatch)  # the whole chain is down -> serve cache

    result = prices.fetch_price_history("1w", refresh=True)

    # Cached series keeps the chart alive...
    assert result["history"]["AAPL"]["closes"] == [209.0, 211.5]
    assert result["cached"] == ["AAPL"]
    # ...and the upstream error is still surfaced (non-fatal).
    assert any("429" in err for err in result["errors"])


def test_fresh_cache_skips_provider_entirely(tmp_path, monkeypatch):
    """A fresh cached series answers page loads without any provider call."""
    _use_fmp(tmp_path, monkeypatch)
    d1, d2 = _days_ago(2), _days_ago(1)

    def ok_get(path, params, *args, **kwargs):
        return [{"date": d2, "price": 211.5}, {"date": d1, "price": 209.0}], None

    monkeypatch.setattr(fmp_provider, "_get", ok_get)
    prices.fetch_price_history("1w")

    def must_not_be_called(*args, **kwargs):
        raise AssertionError("provider should not be called when cache is fresh")

    monkeypatch.setattr(fmp_provider, "_get", must_not_be_called)

    result = prices.fetch_price_history("1w")

    assert result["history"]["AAPL"]["closes"] == [209.0, 211.5]
    assert result["cached"] == ["AAPL"]
    assert result["errors"] == []


def test_stale_cache_refetches_from_provider(tmp_path, monkeypatch):
    """Cached data older than the freshness window triggers a provider pass."""
    _use_fmp(tmp_path, monkeypatch)
    stale1, stale2 = _days_ago(12), _days_ago(11)
    db.cache_price_history({"AAPL": {"dates": [stale1, stale2], "closes": [200.0, 201.0]}})

    d1, d2 = _days_ago(2), _days_ago(1)

    def ok_get(path, params, *args, **kwargs):
        return [{"date": d2, "price": 211.5}, {"date": d1, "price": 209.0}], None

    monkeypatch.setattr(fmp_provider, "_get", ok_get)

    result = prices.fetch_price_history("1w")

    # Fresh provider data replaces the stale cache in the response.
    assert result["history"]["AAPL"]["closes"] == [209.0, 211.5]
    assert result["cached"] == []


def test_get_cached_price_history_filters_by_start_and_needs_two_points(tmp_path):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()
    db.cache_price_history(
        {"MSFT": {"dates": ["2026-01-01", "2026-02-01", "2026-03-01"], "closes": [100, 110, 120]}}
    )

    trimmed = db.get_cached_price_history(["MSFT"], "2026-02-01")
    assert trimmed["MSFT"]["dates"] == ["2026-02-01", "2026-03-01"]
    assert trimmed["MSFT"]["closes"] == [110, 120]

    # A single cached point is treated as a miss (omitted) so callers re-fetch.
    db.cache_price_history({"NVDA": {"dates": ["2026-02-01"], "closes": [500]}})
    assert "NVDA" not in db.get_cached_price_history(["NVDA"])


def test_cache_upsert_overwrites_same_day(tmp_path):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()
    db.cache_price_history({"GOOG": {"dates": ["2026-05-01", "2026-05-02"], "closes": [150.0, 151.0]}})
    db.cache_price_history({"GOOG": {"dates": ["2026-05-02"], "closes": [155.0]}})

    out = db.get_cached_price_history(["GOOG"])
    assert out["GOOG"]["dates"] == ["2026-05-01", "2026-05-02"]
    assert out["GOOG"]["closes"] == [150.0, 155.0]


def test_fetch_symbol_history_falls_back_to_cache(tmp_path, monkeypatch):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()
    d1, d2 = _days_ago(3), _days_ago(2)
    db.cache_price_history({"TSLA": {"dates": [d1, d2], "closes": [250.0, 260.0]}})

    monkeypatch.setattr(settings, "market_data_provider", "fmp")
    monkeypatch.setattr(settings, "fmp_api_key", "test-key")
    # Whole chain rate-limited -> the cached series keeps the chart alive.
    monkeypatch.setattr(fmp_provider, "_get", lambda *a, **k: (None, "429 Too Many Requests"))
    _mute_yahoo(monkeypatch)

    result = prices.fetch_symbol_history("TSLA", "stock", "1y")

    assert result["dates"] == [d1, d2]
    assert result["closes"] == [250.0, 260.0]


# --- symbols no provider can ever serve ------------------------------------
# A private placement like SPCX (SpaceX, offered by Robinhood) sits in a real
# portfolio and in no market-data catalogue. Every provider in the chain was
# asked for it on every page load, failed, retried across its mirrors, backed
# off, and failed again — measured at ~7.9s per dashboard render with the cache
# otherwise fully warm, and the user reading it as "the app is broken".


def test_a_symbol_the_whole_chain_refuses_is_not_asked_again(monkeypatch, tmp_path):
    from backend import db, prices

    db.set_db_path(tmp_path / "miss.db")
    db.init_db()
    db.create_position(PositionIn(symbol="SPCX", broker="manual", asset_type="stock",
                                 quantity=1, average_cost=135, current_price=135))

    attempts = []

    class DeadEnd:
        name = "dead-end"

        def fetch_history(self, period, symbols, positions_by_symbol):
            attempts.append(list(symbols))
            return {"history": {}, "errors": [f"{s}: not found" for s in symbols]}

    monkeypatch.setattr(
        prices.connectors, "market_data_chain", lambda: [("dead-end", DeadEnd())]
    )
    monkeypatch.setattr(prices.connectors, "active_crypto_data", lambda: None)

    prices.fetch_price_history(period="1y")
    assert len(attempts) == 1, "first load should try"
    prices.fetch_price_history(period="1y")
    prices.fetch_price_history(period="1y")
    assert len(attempts) == 1, (
        f"the chain was walked {len(attempts)} times for a symbol it cannot serve"
    )


def test_an_explicit_refresh_still_tries_a_previously_failed_symbol(monkeypatch, tmp_path):
    """Refresh is the user asking. A ticker may have listed since, and a
    negative cache that ignores a direct request is a bug they cannot clear."""
    from backend import db, prices

    db.set_db_path(tmp_path / "miss2.db")
    db.init_db()
    db.create_position(PositionIn(symbol="SPCX", broker="manual", asset_type="stock",
                                 quantity=1, average_cost=135, current_price=135))

    attempts = []

    class DeadEnd:
        name = "dead-end"

        def fetch_history(self, period, symbols, positions_by_symbol):
            attempts.append(list(symbols))
            return {"history": {}, "errors": []}

    monkeypatch.setattr(
        prices.connectors, "market_data_chain", lambda: [("dead-end", DeadEnd())]
    )
    monkeypatch.setattr(prices.connectors, "active_crypto_data", lambda: None)

    prices.fetch_price_history(period="1y")
    prices.fetch_price_history(period="1y", refresh=True)
    assert len(attempts) == 2


def test_a_working_symbol_is_never_marked_unfetchable(monkeypatch, tmp_path):
    from backend import db, prices

    db.set_db_path(tmp_path / "miss3.db")
    db.init_db()
    db.create_position(PositionIn(symbol="AAPL", broker="manual", asset_type="stock",
                                 quantity=1, average_cost=100, current_price=100))

    class Working:
        name = "working"

        def fetch_history(self, period, symbols, positions_by_symbol):
            return {"history": {s: {"dates": ["2026-01-01", "2026-01-02"],
                                    "closes": [1.0, 2.0]} for s in symbols},
                    "errors": []}

    monkeypatch.setattr(
        prices.connectors, "market_data_chain", lambda: [("working", Working())]
    )
    monkeypatch.setattr(prices.connectors, "active_crypto_data", lambda: None)

    prices.fetch_price_history(period="1y")
    assert not prices._recently_unfetchable("AAPL")


def test_one_dead_symbol_does_not_stop_the_others_being_fetched(monkeypatch, tmp_path):
    """The portfolio has SPCX in it; the other nineteen holdings must still
    load normally, both now and on the next request."""
    from backend import db, prices

    db.set_db_path(tmp_path / "miss4.db")
    db.init_db()
    for symbol in ("AAPL", "SPCX"):
        db.create_position(PositionIn(symbol=symbol, broker="manual", asset_type="stock",
                                      quantity=1, average_cost=100, current_price=100))

    class Partial:
        name = "partial"

        def fetch_history(self, period, symbols, positions_by_symbol):
            good = [s for s in symbols if s != "SPCX"]
            return {"history": {s: {"dates": ["2026-01-01", "2026-01-02"],
                                    "closes": [1.0, 2.0]} for s in good},
                    "errors": ["SPCX: not found"] if "SPCX" in symbols else []}

    monkeypatch.setattr(
        prices.connectors, "market_data_chain", lambda: [("partial", Partial())]
    )
    monkeypatch.setattr(prices.connectors, "active_crypto_data", lambda: None)

    out = prices.fetch_price_history(period="1y")
    assert "AAPL" in out["history"]
    assert prices._recently_unfetchable("SPCX")
    assert not prices._recently_unfetchable("AAPL")


def test_the_negative_cache_expires(monkeypatch):
    """A newly listed ticker has to start working without a deploy."""
    import time

    from backend import prices

    prices._history_misses["SPCX"] = time.time() - prices._MISS_TTL_SECONDS - 1
    assert not prices._recently_unfetchable("SPCX")
    prices._history_misses["SPCX"] = time.time()
    assert prices._recently_unfetchable("SPCX")


def test_connector_config_is_not_reread_from_the_database_every_time(tmp_path, monkeypatch):
    """Resolving the active provider walked several config keys, each opening
    its own connection. On managed Postgres every connection re-resolves the
    pooler hostname — one warm price-history request cost 37 connections and
    4.9s of DNS for 14ms of query time."""
    from backend import db
    from backend.connectors import registry

    db.set_db_path(tmp_path / "cfg.db")
    db.init_db()
    registry.forget_instance_cache()

    reads = []
    real = db.get_setting

    def counted(key, default=""):
        reads.append(key)
        return real(key, default)

    monkeypatch.setattr(db, "get_setting", counted)
    for _ in range(10):
        registry._instance_get("connector:yahoo:enabled")
    assert len(reads) == 1, f"read the same config key {len(reads)} times"


def test_saving_config_takes_effect_on_the_very_next_read(tmp_path):
    """A cache that made someone wait 30 seconds for their own API key to
    apply would be worse than the problem it solves."""
    from backend import db
    from backend.connectors import registry

    db.set_db_path(tmp_path / "cfg2.db")
    db.init_db()
    registry.forget_instance_cache()

    registry._instance_set("connector:fmp:enabled", "0")
    assert registry._instance_get("connector:fmp:enabled") == "0"
    registry._instance_set("connector:fmp:enabled", "1")
    assert registry._instance_get("connector:fmp:enabled") == "1", "a write did not invalidate"

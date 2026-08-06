from __future__ import annotations

from datetime import UTC, datetime, timedelta

from backend import db, prices
from backend.config import settings
from backend.models import PositionIn
from backend.providers import fmp as fmp_provider
from backend.providers import yahoo as yahoo_provider


def _mute_yahoo(monkeypatch):
    """Down the keyless Yahoo backstop in the chain so 'provider down' tests
    exercise the cache path deterministically (no real network)."""
    monkeypatch.setattr(yahoo_provider, "_get", lambda *a, **k: (None, "429 Too Many Requests"))


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

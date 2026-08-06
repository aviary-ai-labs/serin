"""Market-data orchestration.

Routes refresh/history/quote requests to the **active market-data connector**,
resolved by the connector registry (portal config) with a settings/env
fallback. Provider implementations live under ``backend.providers`` and are
wrapped by connectors under ``backend.connectors.market_data``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from backend import connectors, db
from backend.models import Position
from backend.providers import fmp as fmp_module


def _period_start_date(period: str) -> str:
    """Earliest date to keep for a period, mirroring the FMP provider window.

    Used to trim the local cache to the requested range; provider-agnostic so
    any connector's cached series filters consistently.
    """
    days = {
        "1w": 10,
        "1m": 35,
        "3m": 100,
        "ytd": max(10, datetime.now(UTC).timetuple().tm_yday + 5),
        "1y": 370,
        "max": 365 * 6,
    }.get(period.lower(), 100)
    return (datetime.now(UTC) - timedelta(days=days)).date().isoformat()


def fmp_symbol(symbol: str, asset_type: str) -> str:
    """Back-compat shim — symbol normalization is provider-specific now."""
    return fmp_module.fmp_symbol(symbol, asset_type)


def _no_provider_result(**extra) -> dict:
    return {
        "provider": "none",
        "updated": 0,
        "symbols": [],
        "errors": [
            "No market-data provider available. Set FMP_API_KEY (or enable a "
            "market-data connector in the portal) — or leave "
            "SERIN_MARKET_DATA_PROVIDER=auto to use the free Yahoo fallback."
        ],
        **extra,
    }


def refresh_prices(symbols: set[str] | None = None) -> dict:
    """Refresh quotes for all priceable positions.

    Crypto positions route through the crypto-specialist layer (CoinGecko)
    when one is enabled; everything else goes to the main provider.
    """
    positions = [
        position for position in db.list_positions()
        if position.asset_type not in ("cash", "option")
    ]
    if symbols is not None:
        wanted = {s.upper() for s in symbols}
        positions = [position for position in positions if position.symbol in wanted]

    provider_name = connectors.active_market_data_id()
    if not positions:
        return {"provider": provider_name, "updated": 0, "symbols": [], "errors": []}

    crypto_connector = connectors.active_crypto_data()
    crypto = [p for p in positions if p.asset_type == "crypto"] if crypto_connector else []
    main = [p for p in positions if p not in crypto]

    prices: dict[str, tuple[float, str]] = {}
    errors: list[str] = []

    if main:
        connector = connectors.active_market_data()
        if connector is None:
            if not crypto:
                return _no_provider_result()
            errors.append("No main market-data provider — only crypto refreshed via CoinGecko")
        else:
            result = connector.refresh_prices(main)
            prices.update(result.get("prices", {}))
            errors.extend(result.get("errors", []))

    if crypto:
        result = crypto_connector.refresh_prices(crypto)
        prices.update(result.get("prices", {}))
        errors.extend(result.get("errors", []))
        provider_name = f"{provider_name}+coingecko" if main else "coingecko"

    updated = db.update_prices(prices)
    return {
        "provider": provider_name,
        "updated": updated,
        "symbols": sorted(prices),
        "errors": errors,
    }


# Cached daily closes count as fresh if the newest point is within this many
# calendar days (covers weekends + market holidays without trading-calendar
# logic) — fresh symbols skip the provider entirely.
_CACHE_FRESH_DAYS = 4
# ...and the oldest point must reach back to (roughly) the requested period
# start, so a 3m-deep cache doesn't masquerade as a MAX-period answer.
_CACHE_COVERAGE_GRACE_DAYS = 10


def _cache_is_fresh(series: dict, start_date: str) -> bool:
    dates = series.get("dates") or []
    if len(dates) < 2:
        return False
    today = datetime.now(UTC).date()
    newest_ok = dates[-1] >= (today - timedelta(days=_CACHE_FRESH_DAYS)).isoformat()
    start = datetime.fromisoformat(start_date).date()
    oldest_ok = dates[0] <= (start + timedelta(days=_CACHE_COVERAGE_GRACE_DAYS)).isoformat()
    return newest_ok and oldest_ok


def _effective_period(symbol: str, period: str, start_date: str, bounds: dict) -> str:
    """The window to actually request from a provider — never re-pulling dates
    already in the cache.

    Full ``period`` when we have no cache or lack coverage back to
    ``start_date`` (need an older backfill); otherwise just the recent gap since
    the latest cached point, mapped to the smallest period key that covers it.
    """
    span = bounds.get(symbol.upper())
    if not span:
        return period  # nothing cached — pull the whole window
    if span["earliest"] > start_date:
        return period  # missing older data — backfill the full window
    try:
        gap = (datetime.now(UTC).date() - date.fromisoformat(span["latest"])).days
    except ValueError:
        return period
    if gap <= 8:
        return "1w"
    if gap <= 33:
        return "1m"
    if gap <= 100:
        return "3m"
    if gap <= 370:
        return "1y"
    return period


def fetch_price_history(period: str = "3m", refresh: bool = False) -> dict:
    """Portfolio-wide daily closes for the trend chart + analytics.

    Cache-first: symbols whose cached series is fresh (newest point within
    ~4 days, coverage back to the period start) are served locally without a
    provider call — this endpoint runs on every page load and previously
    hammered rate-limited providers with 10+ doomed requests per load.
    ``refresh=True`` (the explicit Refresh action) forces a provider pass;
    provider failures still fall back to whatever the cache has.
    """
    positions = [
        position
        for position in db.list_positions()
        if position.asset_type not in {"cash", "option"}
    ]
    symbols = sorted({position.symbol for position in positions})
    provider_name = connectors.active_market_data_id()

    if not symbols:
        return {"period": period, "provider": provider_name, "history": {}, "errors": []}

    start_date = _period_start_date(period)
    cached = db.get_cached_price_history(symbols, start_date)

    if refresh:
        to_fetch = list(symbols)
    else:
        to_fetch = [
            symbol for symbol in symbols
            if symbol not in cached or not _cache_is_fresh(cached[symbol], start_date)
        ]

    fetched: dict[str, dict] = {}
    errors: list[str] = []
    if to_fetch:
        positions_by_symbol: dict[str, Position] = {}
        for position in db.list_positions():
            positions_by_symbol.setdefault(position.symbol, position)

        # Only pull dates we don't already have: a symbol with cached coverage
        # back to the window start is fetched for just the recent gap.
        bounds = db.cached_history_bounds(symbols)

        def _fetch_from(conn, wanted: list[str]) -> list[str]:
            """Fetch ``wanted`` from one connector, bucketed by the incremental
            window each symbol needs. Fills ``fetched``; returns the symbols
            still missing so the next provider in the chain can try them."""
            buckets: dict[str, list[str]] = {}
            for symbol in wanted:
                buckets.setdefault(_effective_period(symbol, period, start_date, bounds), []).append(symbol)
            for eff_period, syms in buckets.items():
                try:
                    result = conn.fetch_history(eff_period, syms, positions_by_symbol)
                except Exception as exc:  # network blow-up — try the next provider / cache
                    errors.append(f"history fetch failed: {exc}")
                    continue
                for sym, series in (result.get("history") or {}).items():
                    if series.get("dates"):
                        fetched[sym] = series
                errors.extend(result.get("errors") or [])
            return [s for s in wanted if s not in fetched]

        # Crypto rides the specialist layer; equities/ETF go down the provider
        # chain (active first, falling through on gaps like FMP 402 / Yahoo 429).
        crypto_connector = connectors.active_crypto_data()
        crypto_fetch = [
            s for s in to_fetch
            if crypto_connector is not None
            and positions_by_symbol.get(s) is not None
            and positions_by_symbol[s].asset_type == "crypto"
        ]
        main_fetch = [s for s in to_fetch if s not in crypto_fetch]

        if crypto_fetch and crypto_connector is not None:
            _fetch_from(crypto_connector, crypto_fetch)

        if main_fetch:
            chain = connectors.market_data_chain()
            if not chain:
                if not cached and not crypto_fetch:
                    return {"period": period, "provider": "none", "history": {}, "errors": ["No market-data provider configured"]}
                errors.append("No market-data provider configured — serving cached history")
            else:
                remaining = list(main_fetch)
                for _cid, connector in chain:
                    if not remaining:
                        break
                    remaining = _fetch_from(connector, remaining)

        # Persist fresh provider data so future loads survive rate limits.
        if fetched:
            db.cache_price_history(fetched)

    # Build the response from the merged cache (existing + freshly-fetched
    # tails) so a narrow incremental fetch still returns the full window.
    merged = db.get_cached_price_history(symbols, start_date) if fetched else cached
    history: dict[str, dict] = {}
    cached_symbols: list[str] = []
    for symbol in symbols:
        key = symbol.upper()
        series = merged.get(key) or (fetched.get(symbol) if symbol in fetched else None)
        if series is None:
            continue
        history[symbol] = series
        if symbol not in fetched and key not in fetched:
            cached_symbols.append(symbol)

    return {
        "period": period,
        "provider": provider_name,
        "history": history,
        "errors": errors,
        "cached": sorted(cached_symbols),
    }


def fetch_quote(symbol: str, asset_type: str = "stock") -> dict | None:
    """Rich single-symbol quote: price, day change, 52-week range, volume.

    Crypto tries the specialist layer first; everything then falls down the
    market-data chain (active provider → fallbacks → keyless Yahoo).
    """
    if asset_type == "crypto":
        crypto_connector = connectors.active_crypto_data()
        if crypto_connector is not None:
            quote = crypto_connector.quote(symbol, asset_type)
            if quote is not None:
                return quote  # specialist failed -> fall through to the chain
    for _cid, connector in connectors.market_data_chain():
        quote = connector.quote(symbol, asset_type)
        if quote is not None:
            return quote
    return None


def fetch_symbol_history(symbol: str, asset_type: str = "stock", period: str = "1y") -> dict:
    """Single-symbol price history for the stock detail view."""
    provider_name = connectors.active_market_data_id()
    key = symbol.upper()

    # Cache-first, same policy as fetch_price_history: a fresh cached series
    # answers immediately instead of burning a rate-limited provider call on
    # every drill-in.
    start_date = _period_start_date(period)
    cached_fresh = db.get_cached_price_history([symbol], start_date).get(key)
    if cached_fresh and _cache_is_fresh(cached_fresh, start_date):
        return {
            "symbol": symbol,
            "period": period,
            "provider": provider_name,
            "dates": cached_fresh["dates"],
            "closes": cached_fresh["closes"],
            "errors": [],
        }

    placeholder = Position(
        id=0, symbol=symbol, name=symbol, broker="manual",
        asset_type=asset_type, quantity=0, average_cost=0, current_price=0,
        sector="", market_value=0, total_cost=0, unrealized_gain=0,
        unrealized_gain_pct=0,
    )
    # Incremental: only pull the missing tail when we already have coverage.
    bounds = db.cached_history_bounds([symbol])
    eff_period = _effective_period(symbol, period, start_date, bounds)
    series = {"dates": [], "closes": []}
    errors: list[str] = []

    if asset_type == "crypto":
        crypto_connector = connectors.active_crypto_data()
        if crypto_connector is not None:
            result = crypto_connector.fetch_history(eff_period, [symbol], {symbol: placeholder})
            series = result.get("history", {}).get(symbol, series)
            errors = result.get("errors", [])

    if not series.get("dates"):
        for _cid, connector in connectors.market_data_chain():
            try:
                result = connector.fetch_history(eff_period, [symbol], {symbol: placeholder})
            except Exception as exc:
                errors = [f"history fetch failed: {exc}"]
                continue
            found = result.get("history", {}).get(symbol)
            if found and found.get("dates"):
                series = found
                errors = result.get("errors", [])
                break
            errors = result.get("errors", errors)

    if series.get("dates"):
        db.cache_price_history({key: series})

    # Merge with the cache so an incremental tail fetch still returns the full
    # window, and a rate-limited provider keeps the chart alive from cache.
    merged = db.get_cached_price_history([symbol], start_date).get(key)
    if merged and merged.get("dates"):
        series = merged

    return {
        "symbol": symbol,
        "period": period,
        "provider": provider_name,
        "dates": series.get("dates", []),
        "closes": series.get("closes", []),
        "errors": errors,
    }

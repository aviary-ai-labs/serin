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


# How long a cached quote serves a user-triggered refresh before we go back to
# the provider. The point of the shared cache is that a hundred people holding
# AAPL cost one call, not a hundred — which only holds if a refresh is willing
# to answer from cache. The scheduled deployment-wide pass ignores this and
# always fetches.
QUOTE_FRESH_SECONDS = 900


def _fetch_quotes(positions: list[Position]) -> tuple[dict[str, tuple[float, str]], list[str], str]:
    """Ask the providers for these positions' prices. Crypto routes to the
    crypto specialist (CoinGecko) when one is enabled; the rest to the main
    market-data connector."""
    provider_name = connectors.active_market_data_id()
    crypto_connector = connectors.active_crypto_data()
    crypto = [p for p in positions if p.asset_type == "crypto"] if crypto_connector else []
    main = [p for p in positions if p not in crypto]

    prices: dict[str, tuple[float, str]] = {}
    errors: list[str] = []

    if main:
        connector = connectors.active_market_data()
        if connector is None:
            if not crypto:
                return {}, [], "none"
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

    return prices, errors, provider_name


def _is_fresh(updated_at: str, within_seconds: int) -> bool:
    try:
        stamp = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return (datetime.now(UTC) - stamp).total_seconds() < within_seconds


def refresh_prices(symbols: set[str] | None = None, max_age_seconds: int | None = None) -> dict:
    """Bring this user's position prices up to date.

    Reads the shared quote cache first and only asks a provider for symbols
    nobody has priced recently — so on a shared deployment the provider bill
    tracks the number of distinct symbols held, not the number of customers
    holding them. ``max_age_seconds=0`` forces a fetch (the scheduler's
    deployment-wide pass).
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
        return {"provider": provider_name, "updated": 0, "symbols": [], "errors": [], "cached": 0}

    # Positions written before the work-list existed are absent from it. Every
    # refresh re-declares what this user holds, so the universe backfills
    # itself rather than needing a cross-user scan nobody is allowed to run.
    db.track_symbols((p.symbol, p.asset_type) for p in positions)

    window = QUOTE_FRESH_SECONDS if max_age_seconds is None else max_age_seconds
    cached = db.get_cached_quotes((p.symbol, p.asset_type) for p in positions)

    prices: dict[str, tuple[float, str]] = {}
    stale: list[Position] = []
    for position in positions:
        entry = cached.get((position.symbol, position.asset_type))
        if entry and window > 0 and _is_fresh(entry[2], window):
            prices[position.symbol] = (entry[0], entry[1])
        else:
            stale.append(position)
    served_from_cache = len(prices)

    errors: list[str] = []
    if stale:
        fetched, errors, provider_name = _fetch_quotes(stale)
        if provider_name == "none":
            # No provider is only fatal when the cache has nothing either;
            # otherwise a warm cache is exactly what should carry the request.
            if not prices:
                return _no_provider_result(cached=0)
            errors = list(errors) + _no_provider_result()["errors"]
            provider_name = "cache"
            fetched = {}
        by_type = {p.symbol: p.asset_type for p in stale}
        db.cache_quotes(
            (symbol, by_type.get(symbol, "stock"), price, sector)
            for symbol, (price, sector) in fetched.items()
        )
        prices.update(fetched)

    updated = db.update_prices(prices)
    return {
        "provider": provider_name,
        "updated": updated,
        "symbols": sorted(prices),
        "errors": errors,
        "cached": served_from_cache,
    }


def _us_equity_market_open(now: datetime | None = None) -> bool:
    """Roughly whether US equities are trading (9:15–16:15 ET, Mon–Fri).

    The buffer takes the open/close auctions; holidays are deliberately not
    modelled — a sweep on a closed Monday just re-reads unchanged prices,
    which costs a little and breaks nothing. Getting this wrong the other way
    (tz data missing → claim closed) would freeze prices, so absent tzdata we
    claim open.
    """
    try:
        from zoneinfo import ZoneInfo

        now_et = (now or datetime.now(UTC)).astimezone(ZoneInfo("America/New_York"))
    except Exception:
        return True
    if now_et.weekday() >= 5:
        return False
    minutes = now_et.hour * 60 + now_et.minute
    return (9 * 60 + 15) <= minutes <= (16 * 60 + 15)


def refresh_tracked_quotes() -> dict:
    """Price every symbol the deployment holds, once, into the shared cache.

    This is the whole point of the cache: one pass over the union of symbols,
    regardless of how many people hold them. Runs on the scheduler; individual
    refreshes then read what it wrote.

    Off-hours, stock quotes cannot change, so sweeping them would spend the
    provider's daily budget on re-reading Friday's close all weekend — they
    are skipped unless never priced at all (a symbol added on Sunday still
    deserves its first number). Crypto trades around the clock and is priced
    by its own free provider, so it always sweeps.
    """
    tracked = db.list_tracked_symbols()
    if not tracked:
        return {"provider": connectors.active_market_data_id(), "symbols": 0, "skipped": 0, "errors": []}

    cached = db.get_cached_quotes(tracked)
    market_open = _us_equity_market_open()
    work = [
        (symbol, asset_type)
        for symbol, asset_type in tracked
        if asset_type == "crypto" or market_open or (symbol, asset_type) not in cached
    ]
    skipped = len(tracked) - len(work)
    if not work:
        return {"provider": connectors.active_market_data_id(), "symbols": 0, "skipped": skipped, "errors": []}

    # The providers take positions; the cache only knows symbols. Stand-ins
    # carry the two fields any provider reads: symbol and asset_type.
    stand_ins = [
        Position(id=0, symbol=symbol, name=symbol, broker="", asset_type=asset_type, quantity=0.0)
        for symbol, asset_type in work
    ]
    prices, errors, provider_name = _fetch_quotes(stand_ins)
    if provider_name == "none":
        return {"provider": "none", "symbols": 0, "skipped": skipped, "errors": _no_provider_result()["errors"]}

    by_type = dict(tracked)
    written = db.cache_quotes(
        (symbol, by_type.get(symbol, "stock"), price, sector)
        for symbol, (price, sector) in prices.items()
    )
    return {"provider": provider_name, "symbols": written, "skipped": skipped, "errors": errors}


def refresh_tracked_history() -> dict:
    """Top up the shared daily-close cache for every tracked symbol.

    Runs once per trading day, after the US close (the scheduler gates the
    timing). Each symbol costs at most one provider call — `` fetch_symbol_history``
    asks ``_effective_period`` for the smallest window covering the gap since
    the newest cached point, so an up-to-date symbol pulls a week's tail, and
    only a brand-new one pulls a full year. Charts then render from cache
    instead of paying the provider at view time.
    """
    tracked = db.list_tracked_symbols()
    if not tracked:
        return {"provider": connectors.active_market_data_id(), "symbols": 0, "skipped": 0, "errors": []}

    today = datetime.now(UTC).date().isoformat()
    bounds = db.cached_history_bounds([symbol for symbol, _ in tracked])
    provider_name = connectors.active_market_data_id()
    topped_up = 0
    skipped = 0
    errors: list[str] = []
    for symbol, asset_type in tracked:
        span = bounds.get(symbol.upper())
        if span and span["latest"] >= today:
            skipped += 1  # already has today's close (e.g. a restart re-ran the sweep)
            continue
        # A symbol's first pull takes full depth: the provider charges one
        # call per request regardless of window, and the MAX chart view needs
        # more than a year. Crypto stays at 1y — CoinGecko's free history
        # stops there. After bootstrap, the incremental tail takes over.
        period = "1y" if (span or asset_type == "crypto") else "max"
        result = fetch_symbol_history(symbol, asset_type, period=period, force=True)
        if result.get("dates"):
            topped_up += 1
        errors.extend(
            err if err.startswith(f"{symbol}:") else f"{symbol}: {err}"
            for err in result.get("errors", [])
        )
    return {"provider": provider_name, "symbols": topped_up, "skipped": skipped, "errors": errors}


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


def fetch_symbol_history(symbol: str, asset_type: str = "stock", period: str = "1y", force: bool = False) -> dict:
    """Single-symbol price history for the stock detail view.

    ``force`` skips the freshness early-return — the daily history sweep wants
    today's close even while yesterday's still counts as "fresh". The
    incremental-tail logic below still applies, so a forced fetch pulls days,
    not a year.
    """
    provider_name = connectors.active_market_data_id()
    key = symbol.upper()

    # Cache-first, same policy as fetch_price_history: a fresh cached series
    # answers immediately instead of burning a rate-limited provider call on
    # every drill-in.
    start_date = _period_start_date(period)
    cached_fresh = db.get_cached_price_history([symbol], start_date).get(key)
    if not force and cached_fresh and _cache_is_fresh(cached_fresh, start_date):
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

"""Yahoo Finance market-data provider.

Free, no API key required. Uses Yahoo's public chart endpoint (v8) for both
quotes and history. Crypto symbols are mapped to Yahoo's ``BTC-USD`` convention.

This provider is deliberately the free fallback so Serin works out-of-the-box
without an FMP subscription — the same baseline Ghostfolio gives self-hosters.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from backend.models import Position


def _symbol(symbol: str, asset_type: str) -> str:
    """Map a Serin symbol to Yahoo's ticker format."""
    ticker = symbol.upper().strip()
    if asset_type == "crypto" and "-" not in ticker:
        # BTCUSD -> BTC-USD
        if len(ticker) >= 6 and ticker.endswith(("USD", "USDT")):
            base = ticker[:-3] if ticker.endswith("USD") else ticker[:-4]
            quote = "USD" if ticker.endswith("USD") else "USDT"
            return f"{base}-{quote}"
    return ticker


_PERIOD_RANGE = {
    "1w": "5d",
    "1m": "1mo",
    "3m": "3mo",
    "6m": "6mo",
    "ytd": "ytd",
    "1y": "1y",
    "5y": "5y",
    "max": "max",
}


# Yahoo runs identical mirrors on query1/query2 — when one host rate-limits
# (429) or hiccups (5xx / transport error), the other usually answers. We
# rotate hosts with a short backoff instead of failing the first populate.
_HOSTS = (
    "https://query1.finance.yahoo.com",
    "https://query2.finance.yahoo.com",
)
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 0.4

# Injection point so tests (and future callers) can avoid real sleeps.
_sleep = time.sleep


# Yahoo's public endpoints 403 a bare Python UA. A normal browser UA is
# sufficient — no auth, no API key.
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}


def _status_message(status: int) -> str:
    """Yahoo's failures, in words that suggest what to do about them.

    An httpx repr pasted into the chart's empty state told the reader nothing
    except that something with a URL in it went wrong. 429 in particular is
    not a bug in the portfolio — it is the free endpoint declining to serve.
    """
    if status == 429:
        return (
            "Yahoo is rate-limiting price requests (HTTP 429). "
            "Configure a market-data key to stop depending on it."
        )
    if status in (401, 403):
        return f"Yahoo refused the request (HTTP {status})."
    if status == 404:
        return "Yahoo has no data for this symbol."
    return f"Yahoo request failed (HTTP {status})."


def _get(path: str, params: dict[str, str]) -> tuple[Any | None, str | None]:
    """GET ``path`` with host failover + light retry.

    Attempts rotate across the query1/query2 mirrors (q1 → q2 → q1) with a
    small increasing backoff, but only for retryable failures — HTTP 429/5xx
    and transport errors. Anything else (404, auth, parse) fails immediately.
    """
    try:
        import httpx
    except Exception as exc:
        return None, f"Yahoo unavailable: {exc!r}"

    headers = _HEADERS

    last_error = "Yahoo request failed"
    for attempt in range(_MAX_ATTEMPTS):
        host = _HOSTS[attempt % len(_HOSTS)]
        try:
            response = httpx.get(
                f"{host}{path}", params=params, headers=headers, timeout=20, follow_redirects=True
            )
            response.raise_for_status()
            return response.json(), None
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            last_error = _status_message(status)
            if status not in _RETRYABLE_STATUSES:
                return None, last_error
        except httpx.TransportError as exc:
            last_error = f"Yahoo request failed: {exc!r}"
        except Exception as exc:
            return None, f"Yahoo request failed: {exc!r}"
        if attempt < _MAX_ATTEMPTS - 1:
            _sleep(_BACKOFF_BASE_SECONDS * (attempt + 1))
    return None, last_error


def _chart(symbol: str, range_: str, interval: str = "1d") -> tuple[dict | None, str | None]:
    payload, error = _get(
        f"/v8/finance/chart/{symbol}",
        {"range": range_, "interval": interval, "includePrePost": "false"},
    )
    if error:
        return None, error
    try:
        result = (payload or {}).get("chart", {}).get("result")
        if not result:
            return None, "no chart data"
        return result[0], None
    except Exception as exc:
        return None, f"Yahoo parse failed: {exc!r}"


# --- fundamentals (quoteSummary) ---------------------------------------------
# Unlike the open chart endpoint, Yahoo's quoteSummary requires a session
# cookie + crumb. We bootstrap once (fc.yahoo.com sets the cookie, then
# /v1/test/getcrumb returns the crumb), cache the pair module-wide, and
# refresh on auth failure.

_CRUMB_TTL_SECONDS = 6 * 3600
_crumb_cache: dict[str, Any] = {"cookies": None, "crumb": None, "at": 0.0}

_FUND_MODULES = "price,summaryDetail,defaultKeyStatistics,fundProfile"
_QUOTE_TYPE_KIND = {"EQUITY": "stock", "ETF": "etf", "MUTUALFUND": "fund"}


def _get_crumb(force: bool = False) -> tuple[Any | None, str | None]:
    """Return (cookies, crumb) for quoteSummary auth, or (None, None).

    Rotates the query1/query2 mirrors with the same backoff as ``_get`` —
    getcrumb rate-limits (429) exactly like the chart endpoint does.
    """
    try:
        import httpx
    except Exception:
        return None, None
    now = time.time()
    if not force and _crumb_cache["crumb"] and now - _crumb_cache["at"] < _CRUMB_TTL_SECONDS:
        return _crumb_cache["cookies"], _crumb_cache["crumb"]
    try:
        # fc.yahoo.com 404s by design — it exists to set the session cookie.
        boot = httpx.get("https://fc.yahoo.com", headers=_HEADERS, timeout=15, follow_redirects=True)
    except Exception:
        return None, None
    for attempt in range(_MAX_ATTEMPTS):
        host = _HOSTS[attempt % len(_HOSTS)]
        try:
            response = httpx.get(
                f"{host}/v1/test/getcrumb", headers=_HEADERS, cookies=boot.cookies, timeout=15
            )
            crumb = response.text.strip()
            if response.status_code == 200 and crumb and "<" not in crumb and " " not in crumb:
                _crumb_cache.update({"cookies": boot.cookies, "crumb": crumb, "at": now})
                return boot.cookies, crumb
            if response.status_code not in _RETRYABLE_STATUSES:
                return None, None
        except Exception:
            pass
        if attempt < _MAX_ATTEMPTS - 1:
            _sleep(_BACKOFF_BASE_SECONDS * (attempt + 1))
    return None, None


def _quote_summary(symbol: str) -> dict | None:
    """quoteSummary ``result[0]`` for ``symbol``, retrying once on a stale crumb."""
    try:
        import httpx
    except Exception:
        return None
    for attempt in (0, 1):
        cookies, crumb = _get_crumb(force=attempt > 0)
        if not crumb:
            return None
        try:
            response = httpx.get(
                f"{_HOSTS[attempt % len(_HOSTS)]}/v10/finance/quoteSummary/{symbol}",
                params={"modules": _FUND_MODULES, "crumb": crumb},
                headers=_HEADERS,
                cookies=cookies,
                timeout=20,
                follow_redirects=True,
            )
            if response.status_code in (401, 403):
                continue  # stale crumb — refresh and retry once
            response.raise_for_status()
            result = (response.json() or {}).get("quoteSummary", {}).get("result")
            return result[0] if result else None
        except Exception:
            return None
    return None


def _raw(node: Any, *path: str) -> float | None:
    """Descend dicts along ``path`` and unwrap Yahoo's {"raw": x, "fmt": ...} leaves."""
    current = node
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    if isinstance(current, dict):
        current = current.get("raw")
    if isinstance(current, bool) or not isinstance(current, (int, float)):
        return None
    if current != current:  # NaN
        return None
    return float(current)


def parse_fundamentals(summary: dict, symbol: str) -> dict | None:
    """Normalize a quoteSummary payload; None when it carries no metrics."""
    price = summary.get("price") or {}
    detail = summary.get("summaryDetail") or {}
    stats = summary.get("defaultKeyStatistics") or {}
    fund = summary.get("fundProfile") or {}
    out = {
        "symbol": symbol.upper(),
        "name": str(price.get("longName") or price.get("shortName") or symbol),
        "kind": _QUOTE_TYPE_KIND.get(str(price.get("quoteType") or "").upper()),
        # marketCap only — an ETF's AUM is not its holdings' size, so no
        # totalAssets fallback; funds simply report None here.
        "market_cap": _raw(price, "marketCap") or _raw(detail, "marketCap"),
        "pe_ratio": _raw(detail, "trailingPE"),
        "pb_ratio": _raw(stats, "priceToBook"),
        "beta": _raw(detail, "beta") or _raw(stats, "beta") or _raw(stats, "beta3Year"),
        # dividendYield / yield arrive as decimals (0.013 = 1.3%).
        "dividend_yield": _raw(detail, "dividendYield")
        or _raw(detail, "yield")
        or _raw(detail, "trailingAnnualDividendYield"),
        "expense_ratio": _raw(fund, "feesExpensesInvestment", "annualReportExpenseRatio"),
    }
    metrics = ("market_cap", "pe_ratio", "pb_ratio", "beta", "dividend_yield", "expense_ratio")
    if all(out[key] is None for key in metrics):
        return None
    return out


def _safe_float(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    if parsed != parsed:  # NaN guard
        return 0.0
    return parsed


@dataclass
class YahooProvider:
    name: str = "yahoo"

    def refresh_prices(self, positions: list[Position]) -> dict:
        prices: dict[str, tuple[float, str]] = {}
        errors: list[str] = []

        by_symbol: dict[str, Position] = {}
        for position in positions:
            by_symbol.setdefault(position.symbol, position)

        for symbol, position in sorted(by_symbol.items()):
            lookup = _symbol(symbol, position.asset_type)
            result, error = _chart(lookup, "5d", "1d")
            if error or not result:
                errors.append(f"{symbol}: {error or 'no chart'}")
                continue
            meta = result.get("meta") or {}
            price = _safe_float(meta.get("regularMarketPrice"))
            if price <= 0:
                price = _safe_float(meta.get("previousClose")) or _safe_float(meta.get("chartPreviousClose"))
            if price <= 0:
                errors.append(f"{symbol}: no Yahoo price")
                continue
            sector = ""  # Yahoo's chart endpoint doesn't expose sector; left to other providers.
            prices[symbol] = (price, sector)

        return {"prices": prices, "errors": errors}

    def fetch_history(
        self,
        period: str,
        symbols: list[str],
        positions_by_symbol: dict[str, Position],
    ) -> dict:
        history: dict[str, dict[str, list]] = {}
        errors: list[str] = []
        range_ = _PERIOD_RANGE.get(period.lower(), "3mo")
        for symbol in symbols:
            position = positions_by_symbol.get(symbol)
            lookup = _symbol(symbol, position.asset_type if position else "stock")
            result, error = _chart(lookup, range_, "1d")
            if error or not result:
                errors.append(f"{symbol}: {error or 'no chart'}")
                continue
            timestamps = result.get("timestamp") or []
            indicators = (result.get("indicators") or {}).get("quote") or [{}]
            closes = indicators[0].get("close") or []
            parsed: list[tuple[str, float]] = []
            for ts, close in zip(timestamps, closes, strict=False):
                value = _safe_float(close)
                if not ts or value <= 0:
                    continue
                day = datetime.fromtimestamp(int(ts), tz=UTC).date().isoformat()
                parsed.append((day, round(value, 6)))
            parsed.sort(key=lambda item: item[0])
            # Dedupe by date (Yahoo occasionally emits dupes at boundaries).
            seen: dict[str, float] = {}
            for day, value in parsed:
                seen[day] = value
            ordered = sorted(seen.items())
            if len(ordered) < 2:
                errors.append(f"{symbol}: no Yahoo price history")
                continue
            history[symbol] = {
                "dates": [item[0] for item in ordered],
                "closes": [item[1] for item in ordered],
            }
        return {"history": history, "errors": errors}

    def fetch_fundamentals(self, symbol: str, asset_type: str = "stock") -> dict | None:
        if asset_type in ("crypto", "cash", "option"):
            return None
        summary = _quote_summary(_symbol(symbol, asset_type))
        if not summary:
            return None
        return parse_fundamentals(summary, symbol)

    def quote(self, symbol: str, asset_type: str) -> dict | None:
        lookup = _symbol(symbol, asset_type)
        result, _ = _chart(lookup, "1y", "1d")
        if not result:
            return None
        meta = result.get("meta") or {}
        price = _safe_float(meta.get("regularMarketPrice"))
        prev_close = _safe_float(meta.get("chartPreviousClose") or meta.get("previousClose"))
        if price <= 0:
            price = prev_close
        if price <= 0:
            return None
        day_change = price - prev_close if prev_close > 0 else 0.0
        day_change_pct = (day_change / prev_close * 100) if prev_close > 0 else 0.0
        # 52-week derive from history.
        indicators = (result.get("indicators") or {}).get("quote") or [{}]
        highs = indicators[0].get("high") or []
        lows = indicators[0].get("low") or []
        year_high = max((_safe_float(high) for high in highs), default=0.0)
        year_low = min((_safe_float(low) for low in lows if _safe_float(low) > 0), default=0.0)
        volumes = indicators[0].get("volume") or []
        volume = _safe_float(volumes[-1]) if volumes else 0.0

        return {
            "symbol": symbol,
            "name": str(meta.get("longName") or meta.get("shortName") or symbol),
            "price": price,
            "previous_close": prev_close,
            "day_change": round(day_change, 6),
            "day_change_pct": round(day_change_pct, 4),
            "day_high": _safe_float(meta.get("regularMarketDayHigh")),
            "day_low": _safe_float(meta.get("regularMarketDayLow")),
            "year_high": year_high,
            "year_low": year_low,
            "volume": volume,
            "market_cap": _safe_float(meta.get("marketCap")),
            "sector": "",
            "currency": str(meta.get("currency") or "USD"),
            "provider": "yahoo",
        }


def provider() -> YahooProvider:
    return YahooProvider()

"""Alpha Vantage market-data connector — free-key stocks/ETF quotes + history.

A resilient alternative/fallback to FMP for equities and ETFs: a free API key
(https://www.alphavantage.co/support/#api-key) covers `GLOBAL_QUOTE` (price) and
`TIME_SERIES_DAILY` (history). The free tier is rate-limited (~25 req/day, 5/min),
so Serin's price-history cache carries most of the load. Crypto stays on the
crypto layer (CoinGecko); this connector focuses on equities/ETF.

Market-data keys fetch *public* prices — they are not broker credentials
(see docs/CONNECTOR-TRUST.md).
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

import httpx

from backend.connectors.base import (
    ConfigField,
    ConnectorManifest,
    MarketDataConnector,
    TestResult,
)
from backend.connectors.registry import register

logger = logging.getLogger(__name__)

_BASE = "https://www.alphavantage.co/query"
_TIMEOUT = 20.0


def _period_start(period: str) -> date | None:
    """UTC cutoff for a period key, or None for 'everything'."""
    today = datetime.now(UTC).date()
    p = (period or "").lower()
    days = {"1w": 7, "1m": 31, "3m": 93, "6m": 186, "1y": 366, "5y": 5 * 366}.get(p)
    if days:
        return today - timedelta(days=days)
    if p == "ytd":
        return date(today.year, 1, 1)
    return None  # max / all / unknown


def _num(value) -> float:
    try:
        return float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return 0.0


def _opt(value) -> float | None:
    """Like ``_num`` but keeps 'missing' honest — 'None'/'-'/'' become None."""
    try:
        parsed = float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None


@register
class AlphaVantageConnector(MarketDataConnector):
    manifest = ConnectorManifest(
        id="alphavantage",
        name="Alpha Vantage",
        kind="market_data",
        description="Stocks & ETFs from Alpha Vantage — a free API key covers quotes and daily history. A resilient alternative to FMP; crypto stays on CoinGecko.",
        icon="ti-chart-histogram",
        docs_url="https://www.alphavantage.co/documentation/",
        default_enabled=False,
        asset_scope="all",
        config_schema=[
            ConfigField(
                key="api_key",
                label="API key",
                type="password",
                secret=True,
                help="Free key at alphavantage.co/support/#api-key. Free tier is rate-limited (~25/day).",
            ),
        ],
    )

    def _key(self) -> str:
        return (self.get("api_key", "") or "").strip()

    def _get(self, params: dict) -> tuple[dict | None, str | None]:
        params = {**params, "apikey": self._key()}
        try:
            resp = httpx.get(_BASE, params=params, timeout=_TIMEOUT)
            resp.raise_for_status()
            payload = resp.json()
        except httpx.HTTPError as exc:
            return None, f"Alpha Vantage request failed: {exc}"
        if not isinstance(payload, dict):
            return None, "Alpha Vantage: unexpected response"
        # Throttling / errors arrive as HTTP 200 with a Note/Information/Error field.
        if payload.get("Note") or payload.get("Information"):
            return None, "Alpha Vantage rate limit reached — try again later."
        if payload.get("Error Message"):
            return None, "Alpha Vantage: unknown symbol or bad request."
        return payload, None

    def refresh_prices(self, positions) -> dict:
        prices: dict[str, tuple[float, str]] = {}
        errors: list[str] = []
        seen: dict[str, object] = {}
        for position in positions:
            seen.setdefault(position.symbol, position)
        for symbol, position in seen.items():
            if getattr(position, "asset_type", "stock") == "crypto":
                errors.append(f"{symbol}: crypto priced via CoinGecko, not Alpha Vantage")
                continue
            payload, error = self._get({"function": "GLOBAL_QUOTE", "symbol": symbol})
            quote = (payload or {}).get("Global Quote") or {}
            price = _num(quote.get("05. price"))
            if error or price <= 0:
                errors.append(f"{symbol}: {error or 'no Alpha Vantage price'}")
                continue
            prices[symbol] = (price, "")
        return {"prices": prices, "errors": errors}

    def fetch_history(self, period, symbols, positions_by_symbol) -> dict:
        history: dict[str, dict[str, list]] = {}
        errors: list[str] = []
        start = _period_start(period)
        outputsize = "compact" if (period or "").lower() in {"1w", "1m", "3m"} else "full"
        for symbol in symbols:
            position = positions_by_symbol.get(symbol)
            if position is not None and getattr(position, "asset_type", "stock") == "crypto":
                errors.append(f"{symbol}: crypto history via CoinGecko, not Alpha Vantage")
                continue
            payload, error = self._get(
                {"function": "TIME_SERIES_DAILY", "symbol": symbol, "outputsize": outputsize}
            )
            series = (payload or {}).get("Time Series (Daily)") or {}
            if error or not series:
                errors.append(f"{symbol}: {error or 'no Alpha Vantage history'}")
                continue
            rows: list[tuple[str, float]] = []
            for day, bar in series.items():
                if start is not None and day < start.isoformat():
                    continue
                close = _num(bar.get("4. close"))
                if close > 0:
                    rows.append((day, round(close, 6)))
            rows.sort(key=lambda item: item[0])
            if len(rows) < 2:
                errors.append(f"{symbol}: not enough Alpha Vantage history")
                continue
            history[symbol] = {"dates": [r[0] for r in rows], "closes": [r[1] for r in rows]}
        return {"history": history, "errors": errors}

    def quote(self, symbol, asset_type) -> dict | None:
        if asset_type == "crypto":
            return None
        payload, error = self._get({"function": "GLOBAL_QUOTE", "symbol": symbol})
        quote = (payload or {}).get("Global Quote") or {}
        price = _num(quote.get("05. price"))
        if error or price <= 0:
            return None
        prev = _num(quote.get("08. previous close"))
        change = _num(quote.get("09. change"))
        change_pct = _num(quote.get("10. change percent"))
        return {
            "symbol": symbol,
            "name": symbol,
            "price": price,
            "previous_close": prev,
            "day_change": round(change, 6),
            "day_change_pct": round(change_pct, 4),
            "day_high": _num(quote.get("03. high")),
            "day_low": _num(quote.get("04. low")),
            "year_high": 0.0,
            "year_low": 0.0,
            "volume": _num(quote.get("06. volume")),
            "market_cap": 0.0,
            "sector": "",
            "currency": "USD",
            "provider": "alphavantage",
        }

    def fetch_fundamentals(self, symbol, asset_type="stock") -> dict | None:
        if asset_type in ("crypto", "cash", "option") or not self._key():
            return None
        payload, error = self._get({"function": "OVERVIEW", "symbol": symbol})
        overview = payload or {}
        if not error and overview.get("Symbol"):
            return {
                "symbol": symbol.upper(),
                "name": str(overview.get("Name") or symbol),
                "kind": "etf" if "etf" in str(overview.get("AssetType", "")).lower() else "stock",
                "market_cap": _opt(overview.get("MarketCapitalization")),
                "pe_ratio": _opt(overview.get("PERatio")),
                "pb_ratio": _opt(overview.get("PriceToBookRatio")),
                "beta": _opt(overview.get("Beta")),
                "dividend_yield": _opt(overview.get("DividendYield")),
                "expense_ratio": None,  # OVERVIEW covers companies, not funds
            }
        # ETFs return an empty OVERVIEW — ETF_PROFILE carries the expense ratio.
        payload, error = self._get({"function": "ETF_PROFILE", "symbol": symbol})
        profile = payload or {}
        expense = _opt(profile.get("net_expense_ratio"))
        dividend_yield = _opt(profile.get("dividend_yield"))
        if error or (expense is None and dividend_yield is None):
            return None
        return {
            "symbol": symbol.upper(),
            "name": symbol,
            "kind": "etf",
            "market_cap": None,
            "pe_ratio": None,
            "pb_ratio": None,
            "beta": None,
            "dividend_yield": dividend_yield,
            "expense_ratio": expense,
        }

    def test(self) -> TestResult:
        if not self._key():
            return TestResult(ok=False, message="Add your Alpha Vantage API key first.")
        payload, error = self._get({"function": "GLOBAL_QUOTE", "symbol": "IBM"})
        if error:
            return TestResult(ok=False, message=error)
        price = _num(((payload or {}).get("Global Quote") or {}).get("05. price"))
        if price > 0:
            return TestResult(ok=True, message=f"Reached Alpha Vantage — IBM at {price:.2f}.")
        return TestResult(ok=False, message="Alpha Vantage returned no price for IBM.")

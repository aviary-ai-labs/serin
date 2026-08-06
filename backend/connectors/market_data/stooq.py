"""Stooq market-data connector — keyless end-of-day history.

Stooq serves free daily OHLC as CSV with **no API key**, which makes it Serin's
zero-config resilience layer: when Yahoo/FMP rate-limit, Stooq still answers.
It's EOD only (last close, not intraday) and US-equity/ETF/index focused
(`aapl.us`, `^spx`); crypto stays on CoinGecko.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

import httpx

from backend.connectors.base import ConnectorManifest, MarketDataConnector, TestResult
from backend.connectors.registry import register

logger = logging.getLogger(__name__)

_DAILY = "https://stooq.com/q/d/l/"
_TIMEOUT = 20.0


def _period_start(period: str) -> date | None:
    today = datetime.now(UTC).date()
    p = (period or "").lower()
    days = {"1w": 7, "1m": 31, "3m": 93, "6m": 186, "1y": 366, "5y": 5 * 366}.get(p)
    if days:
        return today - timedelta(days=days)
    if p == "ytd":
        return date(today.year, 1, 1)
    return None


def _stooq_symbol(symbol: str, asset_type: str) -> str | None:
    ticker = (symbol or "").strip().lower()
    if not ticker or asset_type == "crypto":
        return None  # crypto handled by the CoinGecko layer
    if "." in ticker or ticker.startswith("^"):
        return ticker  # already qualified (e.g. "aapl.us", "^spx")
    return f"{ticker}.us"


def _fetch_daily(stooq_symbol: str, start: date | None) -> tuple[list[tuple[str, float]], str | None]:
    params = {"s": stooq_symbol, "i": "d"}
    if start is not None:
        params["d1"] = start.strftime("%Y%m%d")
        params["d2"] = datetime.now(UTC).date().strftime("%Y%m%d")
    try:
        resp = httpx.get(_DAILY, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        text = resp.text
    except httpx.HTTPError as exc:
        return [], f"Stooq request failed: {exc}"
    lines = text.strip().splitlines()
    if not lines or not lines[0].lower().startswith("date"):
        return [], "Stooq: no data for symbol"
    close_idx = 4  # Date,Open,High,Low,Close,Volume
    rows: list[tuple[str, float]] = []
    for line in lines[1:]:
        cols = line.split(",")
        if len(cols) <= close_idx:
            continue
        day = cols[0].strip()
        try:
            close = float(cols[close_idx])
        except (TypeError, ValueError):
            continue
        if close > 0 and len(day) == 10:
            rows.append((day, round(close, 6)))
    rows.sort(key=lambda item: item[0])
    return rows, None


@register
class StooqConnector(MarketDataConnector):
    manifest = ConnectorManifest(
        id="stooq",
        name="Stooq",
        kind="market_data",
        description="Free, keyless end-of-day history for US stocks, ETFs and indices. Serin's zero-config fallback when other providers rate-limit.",
        icon="ti-database",
        docs_url="https://stooq.com/db/h/",
        default_enabled=False,
        asset_scope="all",
        connect_method="none",  # keyless — nothing to configure
        config_schema=[],
    )

    def refresh_prices(self, positions) -> dict:
        prices: dict[str, tuple[float, str]] = {}
        errors: list[str] = []
        seen: dict[str, object] = {}
        for position in positions:
            seen.setdefault(position.symbol, position)
        for symbol, position in seen.items():
            stq = _stooq_symbol(symbol, getattr(position, "asset_type", "stock"))
            if stq is None:
                errors.append(f"{symbol}: not covered by Stooq (crypto → CoinGecko)")
                continue
            # ~2 weeks is enough to land on the latest close cheaply.
            rows, error = _fetch_daily(stq, datetime.now(UTC).date() - timedelta(days=14))
            if error or not rows:
                errors.append(f"{symbol}: {error or 'no Stooq price'}")
                continue
            prices[symbol] = (rows[-1][1], "")
        return {"prices": prices, "errors": errors}

    def fetch_history(self, period, symbols, positions_by_symbol) -> dict:
        history: dict[str, dict[str, list]] = {}
        errors: list[str] = []
        start = _period_start(period)
        for symbol in symbols:
            position = positions_by_symbol.get(symbol)
            stq = _stooq_symbol(symbol, getattr(position, "asset_type", "stock") if position else "stock")
            if stq is None:
                errors.append(f"{symbol}: not covered by Stooq (crypto → CoinGecko)")
                continue
            rows, error = _fetch_daily(stq, start)
            if error or len(rows) < 2:
                errors.append(f"{symbol}: {error or 'not enough Stooq history'}")
                continue
            history[symbol] = {"dates": [r[0] for r in rows], "closes": [r[1] for r in rows]}
        return {"history": history, "errors": errors}

    def quote(self, symbol, asset_type) -> dict | None:
        stq = _stooq_symbol(symbol, asset_type)
        if stq is None:
            return None
        rows, error = _fetch_daily(stq, datetime.now(UTC).date() - timedelta(days=400))
        if error or len(rows) < 1:
            return None
        price = rows[-1][1]
        prev = rows[-2][1] if len(rows) >= 2 else price
        closes = [r[1] for r in rows]
        day_change = price - prev
        return {
            "symbol": symbol,
            "name": symbol,
            "price": price,
            "previous_close": prev,
            "day_change": round(day_change, 6),
            "day_change_pct": round((day_change / prev * 100) if prev > 0 else 0.0, 4),
            "day_high": 0.0,
            "day_low": 0.0,
            "year_high": max(closes),
            "year_low": min(closes),
            "volume": 0.0,
            "market_cap": 0.0,
            "sector": "",
            "currency": "USD",
            "provider": "stooq",
        }

    def test(self) -> TestResult:
        rows, error = _fetch_daily("aapl.us", datetime.now(UTC).date() - timedelta(days=14))
        if error or not rows:
            return TestResult(ok=False, message=error or "Could not reach Stooq.")
        return TestResult(ok=True, message=f"Reached Stooq — AAPL last close {rows[-1][1]:.2f}.")

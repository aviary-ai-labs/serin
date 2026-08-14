"""Cboe market-data connector — keyless full-depth end-of-day history.

Cboe publishes delayed quotes and complete listed-lifetime daily OHLCV for US
equities/ETFs through its own public chart endpoint (the one powering
cboe.com's charts): ``cdn.cboe.com/api/global/delayed_quotes/charts/historical/
{SYMBOL}.json``. No key, served from a CDN, and — decisively — it covers
symbols other providers paywall per-symbol, at full depth (AAPL reaches back
to 2004; AFRM to its 2021 IPO).

One request returns the whole series; there is no range parameter, so periods
are sliced locally. EOD only — the last row is the newest close, not a live
tick — and crypto stays on CoinGecko.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

import httpx

from backend.connectors.base import ConnectorManifest, MarketDataConnector, TestResult
from backend.connectors.registry import register

logger = logging.getLogger(__name__)

_HISTORY = "https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/{symbol}.json"
_TIMEOUT = 25.0
_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


def _cboe_symbol(symbol: str, asset_type: str) -> str | None:
    ticker = (symbol or "").strip().upper()
    if not ticker or asset_type == "crypto":
        return None  # crypto handled by the CoinGecko layer
    return ticker


def _period_start(period: str) -> date | None:
    today = datetime.now(UTC).date()
    p = (period or "").lower()
    days = {"1w": 7, "1m": 31, "3m": 93, "6m": 186, "1y": 366, "5y": 5 * 366}.get(p)
    if days:
        return today - timedelta(days=days)
    if p == "ytd":
        return date(today.year, 1, 1)
    return None  # "max" and anything unrecognised: the full series


def _fetch_series(cboe_symbol: str) -> tuple[list[dict], str | None]:
    """The symbol's complete daily series, oldest first."""
    try:
        resp = httpx.get(_HISTORY.format(symbol=cboe_symbol), headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
    except httpx.HTTPError as exc:
        return [], f"Cboe request failed: {exc}"
    except ValueError:
        return [], "Cboe: unexpected response (not JSON)"
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        return [], "Cboe: no data for symbol"
    clean = [
        row for row in rows
        if isinstance(row, dict) and row.get("date") and float(row.get("close") or 0) > 0
    ]
    clean.sort(key=lambda row: str(row["date"]))
    return clean, None


@register
class CboeConnector(MarketDataConnector):
    manifest = ConnectorManifest(
        id="cboe",
        name="Cboe",
        kind="market_data",
        description=(
            "Free, keyless daily history for US stocks and ETFs from the Cboe exchange's "
            "public delayed-quotes feed — full listed-lifetime depth, including symbols "
            "other providers paywall. EOD closes, not live ticks."
        ),
        icon="ti-database",
        docs_url="https://www.cboe.com/delayed_quotes/",
        default_enabled=True,
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
            cboe = _cboe_symbol(symbol, getattr(position, "asset_type", "stock"))
            if cboe is None:
                errors.append(f"{symbol}: not covered by Cboe (crypto → CoinGecko)")
                continue
            rows, error = _fetch_series(cboe)
            if error or not rows:
                errors.append(f"{symbol}: {error or 'no Cboe price'}")
                continue
            prices[symbol] = (round(float(rows[-1]["close"]), 6), "")
        return {"prices": prices, "errors": errors}

    def fetch_history(self, period, symbols, positions_by_symbol) -> dict:
        history: dict[str, dict[str, list]] = {}
        errors: list[str] = []
        start = _period_start(period)
        floor = start.isoformat() if start else ""
        for symbol in symbols:
            position = positions_by_symbol.get(symbol)
            cboe = _cboe_symbol(symbol, getattr(position, "asset_type", "stock") if position else "stock")
            if cboe is None:
                errors.append(f"{symbol}: not covered by Cboe (crypto → CoinGecko)")
                continue
            rows, error = _fetch_series(cboe)
            kept = [row for row in rows if str(row["date"]) >= floor] if floor else rows
            if error or len(kept) < 2:
                errors.append(f"{symbol}: {error or 'not enough Cboe history'}")
                continue
            history[symbol] = {
                "dates": [str(row["date"]) for row in kept],
                "closes": [round(float(row["close"]), 6) for row in kept],
            }
        return {"history": history, "errors": errors}

    def quote(self, symbol, asset_type) -> dict | None:
        cboe = _cboe_symbol(symbol, asset_type)
        if cboe is None:
            return None
        rows, error = _fetch_series(cboe)
        if error or not rows:
            return None
        last = rows[-1]
        price = round(float(last["close"]), 6)
        prev = round(float(rows[-2]["close"]), 6) if len(rows) >= 2 else price
        year_floor = (datetime.now(UTC).date() - timedelta(days=366)).isoformat()
        year = [float(row["close"]) for row in rows if str(row["date"]) >= year_floor] or [price]
        day_change = price - prev
        return {
            "symbol": symbol,
            "name": symbol,
            "price": price,
            "previous_close": prev,
            "day_change": round(day_change, 6),
            "day_change_pct": round((day_change / prev * 100) if prev > 0 else 0.0, 4),
            "day_high": round(float(last.get("high") or 0), 6),
            "day_low": round(float(last.get("low") or 0), 6),
            "year_high": round(max(year), 6),
            "year_low": round(min(year), 6),
            "volume": float(last.get("volume") or 0),
            "market_cap": 0.0,
            "sector": "",
            "currency": "USD",
            "provider": "cboe",
        }

    def test(self) -> TestResult:
        rows, error = _fetch_series("AAPL")
        if error or not rows:
            return TestResult(ok=False, message=error or "Could not reach Cboe.")
        last = rows[-1]
        return TestResult(
            ok=True,
            message=f"Reached Cboe — {len(rows)} days of AAPL, last close {float(last['close']):.2f} on {last['date']}.",
        )

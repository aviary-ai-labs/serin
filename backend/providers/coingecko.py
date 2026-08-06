"""CoinGecko market-data provider — crypto only.

Free, no API key required (an optional demo key raises the rate limit).
CoinGecko is the crypto-fidelity upgrade over generalist providers: proper
24h stats, market cap, and clean daily closes for thousands of coins.

Scope: **crypto positions only.** The orchestrator in ``backend.prices``
routes crypto to this provider (when the connector is enabled) and leaves
stocks/ETFs on the main provider.

Free-tier note: daily history is capped at 365 days, so "MAX" charts span at
most one year through this provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

BASE_URL = "https://api.coingecko.com/api/v3"

# Curated symbol -> CoinGecko id map for the majors. Anything not listed here
# is resolved dynamically via /coins/markets (which accepts symbols) and the
# resolution is memoized per-process.
SYMBOL_TO_ID: dict[str, str] = {
    "btc": "bitcoin",
    "eth": "ethereum",
    "sol": "solana",
    "ada": "cardano",
    "xrp": "ripple",
    "doge": "dogecoin",
    "dot": "polkadot",
    "ltc": "litecoin",
    "link": "chainlink",
    "avax": "avalanche-2",
    "matic": "matic-network",
    "uni": "uniswap",
    "atom": "cosmos",
    "bnb": "binancecoin",
    "trx": "tron",
    "usdt": "tether",
    "usdc": "usd-coin",
}

_id_cache: dict[str, str] = {}


def _norm(symbol: str) -> str:
    """BTC / BTC-USD / BTCUSD / BTC-USDT -> btc (CoinGecko keys by base coin)."""
    ticker = symbol.upper().strip().replace("-", "")
    for quote in ("USDT", "USD"):
        if len(ticker) > len(quote) and ticker.endswith(quote):
            ticker = ticker[: -len(quote)]
            break
    return ticker.lower()


def _get(path: str, params: dict[str, Any], api_key: str = "") -> tuple[Any | None, str | None]:
    try:
        import httpx
    except Exception as exc:  # pragma: no cover
        return None, f"CoinGecko unavailable: {exc!r}"

    headers = {"Accept": "application/json"}
    if api_key:
        headers["x-cg-demo-api-key"] = api_key
    try:
        response = httpx.get(f"{BASE_URL}{path}", params=params, headers=headers, timeout=20)
        response.raise_for_status()
        return response.json(), None
    except Exception as exc:
        return None, f"CoinGecko request failed: {exc!r}"


def _period_days(period: str) -> int:
    days = {
        "1w": 10,
        "1m": 35,
        "3m": 100,
        "ytd": max(10, datetime.now(UTC).timetuple().tm_yday + 5),
        "1y": 365,
        # Free tier caps daily history at 365 days.
        "max": 365,
    }.get(period.lower(), 100)
    return min(days, 365)


@dataclass
class CoinGeckoProvider:
    name: str = "coingecko"
    api_key: str = ""

    def _markets(self, symbols: list[str]) -> tuple[list[dict], list[str]]:
        """/coins/markets rows for the given Serin symbols (crypto)."""
        norms = sorted({_norm(s) for s in symbols})
        if not norms:
            return [], []
        payload, error = _get(
            "/coins/markets",
            {"vs_currency": "usd", "symbols": ",".join(norms), "per_page": 250},
            self.api_key,
        )
        if error:
            return [], [error]
        rows = payload if isinstance(payload, list) else []
        for row in rows:  # memoize symbol -> id resolutions as we see them
            sym, cg_id = str(row.get("symbol") or "").lower(), str(row.get("id") or "")
            if sym and cg_id:
                _id_cache.setdefault(sym, cg_id)
        return rows, []

    def _resolve_id(self, symbol: str) -> str | None:
        norm = _norm(symbol)
        if norm in SYMBOL_TO_ID:
            return SYMBOL_TO_ID[norm]
        if norm in _id_cache:
            return _id_cache[norm]
        rows, _errors = self._markets([symbol])
        return _id_cache.get(norm)

    def refresh_prices(self, positions) -> dict:
        crypto = [p for p in positions if p.asset_type == "crypto"]
        if not crypto:
            return {"prices": {}, "errors": []}
        rows, errors = self._markets([p.symbol for p in crypto])
        by_norm = {str(row.get("symbol") or "").lower(): row for row in rows}
        prices: dict[str, tuple[float, str]] = {}
        for position in crypto:
            row = by_norm.get(_norm(position.symbol))
            price = float(row.get("current_price") or 0) if row else 0.0
            if price > 0:
                prices[position.symbol] = (price, "Crypto")
            else:
                errors.append(f"{position.symbol}: not found on CoinGecko")
        return {"prices": prices, "errors": errors}

    def fetch_history(self, period: str, symbols, positions_by_symbol) -> dict:
        history: dict[str, dict[str, list]] = {}
        errors: list[str] = []
        days = _period_days(period)
        for symbol in symbols:
            position = positions_by_symbol.get(symbol)
            if position is not None and position.asset_type != "crypto":
                errors.append(f"{symbol}: CoinGecko serves crypto only")
                continue
            cg_id = self._resolve_id(symbol)
            if not cg_id:
                errors.append(f"{symbol}: not found on CoinGecko")
                continue
            payload, error = _get(
                f"/coins/{cg_id}/market_chart",
                {"vs_currency": "usd", "days": days, "interval": "daily"},
                self.api_key,
            )
            if error:
                errors.append(f"{symbol}: {error}")
                continue
            points = (payload or {}).get("prices") or []
            # De-dupe to one close per day (CoinGecko returns a trailing
            # intraday point for "today").
            seen: dict[str, float] = {}
            for point in points:
                try:
                    ts_ms, price = point[0], float(point[1])
                except (TypeError, ValueError, IndexError):
                    continue
                if price <= 0:
                    continue
                day = datetime.fromtimestamp(ts_ms / 1000, tz=UTC).date().isoformat()
                seen[day] = round(price, 6)
            ordered = sorted(seen.items())
            if len(ordered) < 2:
                errors.append(f"{symbol}: no CoinGecko price history")
                continue
            history[symbol] = {
                "dates": [item[0] for item in ordered],
                "closes": [item[1] for item in ordered],
            }
        return {"history": history, "errors": errors}

    def quote(self, symbol: str, asset_type: str) -> dict | None:
        if asset_type != "crypto":
            return None
        rows, _errors = self._markets([symbol])
        row = next((r for r in rows if str(r.get("symbol") or "").lower() == _norm(symbol)), None)
        if not row:
            return None
        price = float(row.get("current_price") or 0)
        if price <= 0:
            return None
        change = float(row.get("price_change_24h") or 0)
        prev = price - change
        return {
            "symbol": symbol,
            "name": str(row.get("name") or symbol),
            "price": price,
            "previous_close": round(prev, 6),
            "day_change": round(change, 6),
            "day_change_pct": float(row.get("price_change_percentage_24h") or 0),
            "day_high": float(row.get("high_24h") or 0),
            "day_low": float(row.get("low_24h") or 0),
            # CoinGecko's markets payload doesn't include 52-week stats.
            "year_high": 0.0,
            "year_low": 0.0,
            "volume": float(row.get("total_volume") or 0),
            "market_cap": float(row.get("market_cap") or 0),
            "sector": "Crypto",
            "currency": "USD",
            "provider": "coingecko",
        }


def provider(api_key: str = "") -> CoinGeckoProvider:
    return CoinGeckoProvider(api_key=api_key)

"""Financial Modeling Prep market-data provider.

Wraps the existing FMP integration so the orchestrator in ``backend.prices``
can dispatch to it via a uniform interface alongside other providers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from backend.config import settings
from backend.models import Position


def _symbol(symbol: str, asset_type: str) -> str:
    """Map a Serin symbol to FMP's ticker format.

    Crypto pairs lose the dash (BTC-USD -> BTCUSD), and bare crypto symbols
    get the USD quote appended (BTC -> BTCUSD) — without it, "BTC" resolves
    to an unrelated *equity* ticker on FMP and returns a wildly wrong price.
    """
    ticker = symbol.upper()
    if asset_type == "crypto":
        ticker = ticker.replace("-", "")
        if not ticker.endswith("USD"):
            ticker += "USD"
        return ticker
    return ticker


def _get(
    path: str,
    params: dict[str, str],
    api_key: str | None = None,
    base_url: str | None = None,
) -> tuple[Any | None, str | None]:
    try:
        import httpx
    except Exception as exc:
        return None, f"FMP unavailable: {exc!r}"

    # DB/portal-supplied key wins; fall back to the FMP_API_KEY env var.
    api_key = (api_key if api_key is not None else settings.fmp_api_key).strip()
    if not api_key:
        return None, "FMP_API_KEY is not configured"
    base = base_url or settings.fmp_base_url
    url = f"{base.rstrip('/')}/{path.lstrip('/')}"
    try:
        response = httpx.get(url, params={**params, "apikey": api_key}, timeout=20)
        response.raise_for_status()
        return response.json(), None
    except Exception as exc:
        return None, f"FMP request failed: {exc!r}"


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        if isinstance(payload.get("historical"), list):
            return [row for row in payload["historical"] if isinstance(row, dict)]
        if isinstance(payload.get("data"), list):
            return [row for row in payload["data"] if isinstance(row, dict)]
        return [payload]
    return []


def _first_number(row: dict[str, Any], keys: tuple[str, ...]) -> float:
    for key in keys:
        value = row.get(key)
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return 0.0


def _period_start(period: str) -> str:
    days = {
        "1w": 10,
        "1m": 35,
        "3m": 100,
        "ytd": max(10, datetime.now(UTC).timetuple().tm_yday + 5),
        "1y": 370,
        "max": 365 * 6,
    }.get(period.lower(), 100)
    return (datetime.now(UTC) - timedelta(days=days)).date().isoformat()


@dataclass
class FMPProvider:
    name: str = "fmp"
    api_key: str | None = None
    base_url: str | None = None

    def _fetch(self, path: str, params: dict[str, str]):
        return _get(path, params, self.api_key, self.base_url)

    def refresh_prices(self, positions: list[Position]) -> dict:
        prices: dict[str, tuple[float, str]] = {}
        errors: list[str] = []

        by_symbol: dict[str, Position] = {}
        for position in positions:
            by_symbol.setdefault(position.symbol, position)

        for symbol, position in sorted(by_symbol.items()):
            lookup = _symbol(symbol, position.asset_type)
            profile_payload, profile_error = self._fetch("stable/profile", {"symbol": lookup})
            if profile_error:
                errors.append(f"{symbol}: {profile_error}")
                continue
            profile = (_rows(profile_payload) or [{}])[0]
            price = _first_number(profile, ("price",))
            sector = str(profile.get("sector") or "").strip()
            if not sector and position.asset_type == "etf":
                sector = str(profile.get("category") or profile.get("industry") or "ETF").strip()

            if price <= 0:
                quote_payload, quote_error = self._fetch("stable/quote", {"symbol": lookup})
                if quote_error:
                    errors.append(f"{symbol}: {quote_error}")
                    continue
                quote = (_rows(quote_payload) or [{}])[0]
                price = _first_number(quote, ("price", "previousClose", "open"))

            if price <= 0:
                errors.append(f"{symbol}: no FMP price")
                continue
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
        start_date = _period_start(period)
        for symbol in symbols:
            position = positions_by_symbol.get(symbol)
            lookup = _symbol(symbol, position.asset_type if position else "stock")
            payload, error = self._fetch(
                "stable/historical-price-eod/light",
                {"symbol": lookup, "from": start_date},
            )
            if error:
                errors.append(f"{symbol}: {error}")
                continue
            rows = _rows(payload)
            parsed: list[tuple[str, float]] = []
            for row in rows:
                date_value = str(row.get("date") or "")[:10]
                close = _first_number(row, ("price", "close", "adjClose"))
                if date_value and close > 0:
                    parsed.append((date_value, round(close, 6)))
            parsed.sort(key=lambda item: item[0])
            if len(parsed) < 2:
                errors.append(f"{symbol}: no FMP price history")
                continue
            history[symbol] = {
                "dates": [item[0] for item in parsed],
                "closes": [item[1] for item in parsed],
            }
        return {"history": history, "errors": errors}

    def fetch_fundamentals(self, symbol: str, asset_type: str = "stock") -> dict | None:
        if asset_type in ("crypto", "cash", "option"):
            return None
        lookup = _symbol(symbol, asset_type)
        payload, error = self._fetch("stable/profile", {"symbol": lookup})
        if error:
            return None
        profile = (_rows(payload) or [{}])[0]
        if not (profile.get("symbol") or profile.get("companyName")):
            return None

        def opt(*keys: str) -> float | None:
            for key in keys:
                try:
                    parsed = float(profile.get(key))
                except (TypeError, ValueError):
                    continue
                if parsed == parsed and parsed != 0:
                    return parsed
            return None

        price = opt("price")
        last_dividend = opt("lastDividend", "lastDiv")  # annual $ per share
        kind = "etf" if profile.get("isEtf") else ("fund" if profile.get("isFund") else "stock")
        out = {
            "symbol": symbol.upper(),
            "name": str(profile.get("companyName") or profile.get("name") or symbol),
            "kind": kind,
            # A fund's AUM is not its holdings' size — caps stay stock-only.
            "market_cap": opt("marketCap") if kind == "stock" else None,
            "pe_ratio": None,  # not in the free profile endpoint
            "pb_ratio": None,
            "beta": opt("beta"),
            "dividend_yield": round(last_dividend / price, 6) if last_dividend and price else None,
            "expense_ratio": None,  # FMP gates fund fee data behind paid plans
        }
        if all(out[key] is None for key in ("market_cap", "beta", "dividend_yield")):
            return None
        return out

    def quote(self, symbol: str, asset_type: str) -> dict | None:
        lookup = _symbol(symbol, asset_type)
        profile_payload, _ = self._fetch("stable/profile", {"symbol": lookup})
        profile = (_rows(profile_payload) or [{}])[0]
        quote_payload, _ = self._fetch("stable/quote", {"symbol": lookup})
        q = (_rows(quote_payload) or [{}])[0]
        price = _first_number(q, ("price", "previousClose", "open"))
        if price <= 0:
            price = _first_number(profile, ("price",))
        if price <= 0:
            return None
        return {
            "symbol": symbol,
            "name": str(profile.get("companyName") or profile.get("name") or symbol),
            "price": price,
            "previous_close": _first_number(q, ("previousClose",)),
            "day_change": _first_number(q, ("change",)),
            "day_change_pct": _first_number(q, ("changesPercentage", "changePercentage")),
            "day_high": _first_number(q, ("dayHigh",)),
            "day_low": _first_number(q, ("dayLow",)),
            "year_high": _first_number(q, ("yearHigh",)),
            "year_low": _first_number(q, ("yearLow",)),
            "volume": _first_number(q, ("volume",)),
            "market_cap": _first_number(q, ("marketCap",)),
            "sector": str(profile.get("sector") or "").strip(),
            "currency": str(profile.get("currency") or "USD").strip() or "USD",
            "provider": "fmp",
        }


def provider() -> FMPProvider:
    return FMPProvider()


# Back-compat re-exports for code paths that imported from prices.py historically
fmp_symbol = _symbol

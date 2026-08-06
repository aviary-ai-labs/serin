"""Foreign-exchange rates for multi-currency aggregation.

Rates are USD-based, fetched from the keyless open.er-api.com endpoint and
cached in SQLite (``fx_rates``) with a 12-hour TTL. On fetch failure the last
cached rates are served regardless of age — a slightly stale FX rate beats a
blank dashboard. If a currency has never been resolvable, conversion falls
back to 1:1 and the caller can surface that in errors.

Snapshot values (market value / cost / gain) are converted; price *history*
stays in the provider's native currency — a documented v1 limitation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from backend import db
from backend.models import utcnow_iso

FX_URL = "https://open.er-api.com/v6/latest/USD"
TTL_HOURS = 12


def _cached_rates() -> tuple[dict[str, float], str | None]:
    """(rates, newest_updated_at) from the local cache."""
    with db.connect() as conn:
        rows = conn.execute("SELECT quote, rate, updated_at FROM fx_rates").fetchall()
    rates = {row["quote"]: float(row["rate"]) for row in rows if float(row["rate"]) > 0}
    newest = max((row["updated_at"] for row in rows), default=None)
    return rates, newest


def _store_rates(rates: dict[str, float]) -> None:
    now = utcnow_iso()
    with db.connect() as conn:
        for quote, rate in rates.items():
            if not isinstance(rate, (int, float)) or rate <= 0:
                continue
            conn.execute(
                """INSERT INTO fx_rates (quote, rate, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(quote) DO UPDATE SET rate=excluded.rate, updated_at=excluded.updated_at""",
                (str(quote).upper(), float(rate), now),
            )


def _fetch_rates() -> dict[str, float] | None:
    try:
        import httpx

        response = httpx.get(FX_URL, timeout=15)
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        rates = payload.get("rates") or {}
        if not isinstance(rates, dict) or "USD" not in rates:
            return None
        return {str(k).upper(): float(v) for k, v in rates.items() if isinstance(v, (int, float)) and v > 0}
    except Exception:
        return None


def get_rates(force: bool = False) -> dict[str, float]:
    """USD-based rates map (``{"USD": 1.0, "EUR": 0.92, ...}``), cache-first."""
    cached, newest = _cached_rates()
    if cached and not force and newest:
        try:
            age = datetime.now(UTC) - datetime.fromisoformat(newest)
            if age < timedelta(hours=TTL_HOURS):
                return cached
        except ValueError:
            pass
    fresh = _fetch_rates()
    if fresh:
        _store_rates(fresh)
        return fresh
    return cached  # stale beats nothing


def convert_factor(from_currency: str, to_currency: str, rates: dict[str, float] | None = None) -> float:
    """Multiplier turning an amount in ``from_currency`` into ``to_currency``.

    Returns 1.0 when the currencies match or a rate is missing (documented
    degrade — aggregation continues rather than erroring the dashboard).
    """
    src = (from_currency or "USD").upper()
    dst = (to_currency or "USD").upper()
    if src == dst:
        return 1.0
    if rates is None:
        rates = get_rates()
    src_rate, dst_rate = rates.get(src), rates.get(dst)
    if not src_rate or not dst_rate:
        return 1.0
    return dst_rate / src_rate


def display_currency() -> str:
    return (db.get_setting("display_currency", "USD") or "USD").upper()


def set_display_currency(code: str) -> str:
    code = (code or "USD").strip().upper()
    if len(code) != 3 or not code.isalpha():
        raise ValueError("currency must be a 3-letter ISO code")
    db.set_setting("display_currency", code)
    return code

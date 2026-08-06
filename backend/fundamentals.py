"""Per-symbol fundamentals with a local cache.

Market-data depth that powers the Stocks detail view and the Intelligence
X-ray (fee drag, factor snapshot). Follows the price-history cache philosophy:
never re-pull what's still fresh. Fundamentals move slowly, so a cached row is
served for ``FRESH_DAYS``; a provider miss is negative-cached for
``MISS_FRESH_DAYS`` so unknown symbols don't burn API budget on every run.

Data comes from the market-data connector chain (``fetch_fundamentals`` is an
optional capability — connectors without it fall through via the base-class
default), so the same trust order and keyless Yahoo backstop apply as for
prices.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from backend import connectors, db

logger = logging.getLogger(__name__)

FRESH_DAYS = 7  # a week-old P/E or expense ratio is fine
MISS_FRESH_DAYS = 1  # retry unknown symbols at most daily
MAX_FETCH_PER_CALL = 25  # bound worst-case first-run latency on big portfolios

# Asset types that have no fundamentals concept — never fetched, never cached.
_SKIP_TYPES = {"crypto", "cash", "option"}


def _age_days(fetched_at: str) -> float:
    try:
        stamp = datetime.fromisoformat(str(fetched_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return float("inf")
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return (datetime.now(UTC) - stamp).total_seconds() / 86400


def get_fundamentals(
    symbols: list[str],
    asset_types: dict[str, str] | None = None,
    max_age_days: float = FRESH_DAYS,
) -> dict[str, dict]:
    """``{SYMBOL: normalized fundamentals}`` for the symbols that have data.

    Serves fresh cache first, then walks the market-data chain for the rest and
    caches whatever comes back (including misses). Symbols nobody can serve are
    simply absent from the result — callers treat fundamentals as best-effort.
    """
    wanted = sorted({(s or "").strip().upper() for s in symbols if s and str(s).strip()})
    types = {str(k).upper(): v for k, v in (asset_types or {}).items()}
    cached = db.get_cached_fundamentals(wanted)

    out: dict[str, dict] = {}
    stale: list[str] = []
    for symbol in wanted:
        row = cached.get(symbol)
        if row:
            payload = row.get("payload") or {}
            window = max_age_days if payload.get("source") else MISS_FRESH_DAYS
            if _age_days(row.get("fetched_at", "")) <= window:
                if payload.get("source"):
                    out[symbol] = payload
                continue
        stale.append(symbol)

    stale = [s for s in stale if types.get(s, "stock") not in _SKIP_TYPES][:MAX_FETCH_PER_CALL]
    if not stale:
        return out

    chain = connectors.market_data_chain()
    for symbol in stale:
        asset_type = types.get(symbol, "stock")
        data: dict | None = None
        for connector_id, connector in chain:
            try:
                data = connector.fetch_fundamentals(symbol, asset_type)
            except Exception as exc:
                logger.debug("fundamentals %s via %s failed: %r", symbol, connector_id, exc)
                data = None
            if data:
                data["source"] = connector_id
                break
        db.upsert_fundamentals(symbol, data or {"symbol": symbol, "source": None})
        if data:
            out[symbol] = data
    return out

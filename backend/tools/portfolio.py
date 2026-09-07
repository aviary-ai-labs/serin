"""The built-in agent tools — read-only views of one portfolio.

Registered on import by :mod:`backend.tools`. Each handler is a thin wrapper
over code that already exists and is already tested: the arithmetic lives in
``backend.analytics``, ``backend.realized`` and ``backend.db``, and none of it
is reimplemented here. What this module adds is shape — deciding *which*
number answers a question, and refusing to hand a model a pile of rows when a
computed answer will do.

Two conventions every tool follows:

- **Freshness travels with the data.** Prices come from a cache that a failed
  refresh leaves standing, so a snapshot with no timestamp invites an agent to
  quote a week-old number as today's. Every tool that reads prices reports
  when they were last updated and how many holdings are stale.
- **Absent, never fabricated.** Following the X-ray precedent: a section with
  no data is omitted or explicitly null, never zero-filled.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from backend import analytics, db, realized
from backend import data_gaps as data_gaps_module
from backend.tools import Tool, ToolError, register

# A price older than this is called out rather than quoted silently. Two days
# covers a normal weekend without crying wolf every Monday morning.
STALE_AFTER_HOURS = 48

# Row-returning tools cap out here. A model given 2,000 transactions will
# summarise them badly; one given 200 and told there are more can ask again
# with a filter.
MAX_ROWS = 200


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _freshness(positions) -> dict[str, Any]:
    """When these prices were last updated, and how much of the book is stale."""
    priced = [p for p in positions if p.asset_type != "cash" and p.updated_at]
    stamps = [ts for ts in (_parse_iso(p.updated_at) for p in priced) if ts]
    if not stamps:
        return {"prices_updated_at": None, "stale_positions": 0, "stale_symbols": []}
    cutoff = datetime.now(UTC) - timedelta(hours=STALE_AFTER_HOURS)
    stale = sorted(
        {p.symbol for p in priced if (_parse_iso(p.updated_at) or datetime.now(UTC)) < cutoff}
    )
    return {
        "prices_updated_at": min(stamps).isoformat(timespec="seconds"),
        "stale_positions": len(stale),
        "stale_symbols": stale[:20],
        "stale_after_hours": STALE_AFTER_HOURS,
    }


def _round(value: Any, places: int = 2) -> Any:
    return round(value, places) if isinstance(value, (int, float)) and not isinstance(value, bool) else value


# ---------------------------------------------------------------------------
# get_portfolio_summary
# ---------------------------------------------------------------------------


def _get_portfolio_summary(top_holdings: int = 10) -> dict[str, Any]:
    summary = db.portfolio_summary()
    positions = summary.positions
    total = summary.total_value or 0.0
    ranked = sorted(
        (p for p in positions if p.asset_type != "cash"),
        key=lambda p: p.market_value,
        reverse=True,
    )
    return {
        "total_value": _round(summary.total_value),
        "total_cost": _round(summary.total_cost),
        "total_gain": _round(summary.total_gain),
        "total_gain_pct": _round(summary.total_gain_pct, 4),
        "cash_value": _round(summary.cash_value),
        "position_count": len(positions),
        "top_holdings": [
            {
                "symbol": p.symbol,
                "name": p.name,
                "market_value": _round(p.market_value),
                "weight_pct": _round((p.market_value / total * 100) if total else 0.0, 2),
                "unrealized_gain": _round(p.unrealized_gain),
                "unrealized_gain_pct": _round(p.unrealized_gain_pct, 4),
            }
            for p in ranked[: max(0, int(top_holdings))]
        ],
        "broker_breakdown": summary.broker_breakdown,
        "sector_breakdown": summary.sector_breakdown,
        "freshness": _freshness(positions),
    }


register(
    Tool(
        name="get_portfolio_summary",
        description=(
            "Total value, cost, unrealised gain, cash, broker and sector breakdown, "
            "and the largest holdings by weight. Start here — it answers most "
            "portfolio questions without needing the full position list."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "top_holdings": {
                    "type": "integer",
                    "description": "How many of the largest holdings to include (default 10).",
                }
            },
            "required": [],
        },
        handler=_get_portfolio_summary,
    )
)


# ---------------------------------------------------------------------------
# get_performance
# ---------------------------------------------------------------------------

PERIODS = ("1D", "WTD", "MTD", "YTD", "1Y", "MAX")


def _get_performance(period: str | None = None, include_nav_series: bool = False) -> dict[str, Any]:
    computed = analytics.period_returns()
    periods = computed.get("periods") or []
    if period:
        wanted = period.strip().upper()
        periods = [row for row in periods if row.get("period") == wanted]
        if not periods and wanted != "1D":
            raise ToolError(
                f"No {wanted} return available — not enough price history. "
                f"Periods with data: {', '.join(r['period'] for r in computed.get('periods') or []) or 'none'}."
            )
    result: dict[str, Any] = {
        "today_change": _round(computed.get("today_change")),
        "today_change_pct": _round(computed.get("today_change_pct"), 4),
        "periods": periods,
        "transaction_accurate": computed.get("accurate"),
        "indicative": computed.get("indicative", True),
        "note": computed.get("note"),
    }
    if include_nav_series:
        result["nav_series"] = computed.get("nav_series") or []
    return result


register(
    Tool(
        name="get_performance",
        description=(
            "Portfolio returns: today, WTD, MTD, YTD, 1Y and max, plus the "
            "transaction-accurate TWR/MWR figures. Period returns are indicative "
            "(today's basket back-priced on historical closes); the "
            "transaction_accurate block is the money-weighted truth. Quote the "
            "note when reporting these."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "period": {
                    "type": "string",
                    "description": "Restrict to one period.",
                    "enum": list(PERIODS),
                },
                "include_nav_series": {
                    "type": "boolean",
                    "description": "Include the daily NAV series (large — only for charting).",
                },
            },
            "required": [],
        },
        handler=_get_performance,
    )
)


# ---------------------------------------------------------------------------
# list_positions / get_position
# ---------------------------------------------------------------------------


def _position_row(position, total: float) -> dict[str, Any]:
    return {
        "symbol": position.symbol,
        "name": position.name,
        "broker": position.broker,
        "asset_type": position.asset_type,
        "quantity": position.quantity,
        "average_cost": _round(position.average_cost, 6),
        "current_price": _round(position.current_price, 6),
        "market_value": _round(position.market_value),
        "weight_pct": _round((position.market_value / total * 100) if total else 0.0, 2),
        "unrealized_gain": _round(position.unrealized_gain),
        "unrealized_gain_pct": _round(position.unrealized_gain_pct, 4),
        "sector": position.sector,
        "updated_at": position.updated_at,
    }


def _list_positions(
    broker: str | None = None,
    asset_type: str | None = None,
    include_closed: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    positions = db.list_positions(include_closed=include_closed)
    if broker:
        positions = [p for p in positions if p.broker.lower() == broker.strip().lower()]
    if asset_type:
        positions = [p for p in positions if p.asset_type.lower() == asset_type.strip().lower()]
    total = sum(p.market_value for p in positions)
    ranked = sorted(positions, key=lambda p: p.market_value, reverse=True)
    capped = max(1, min(int(limit), MAX_ROWS))
    return {
        "count": len(ranked),
        "returned": min(len(ranked), capped),
        "positions": [_position_row(p, total) for p in ranked[:capped]],
        "freshness": _freshness(positions),
    }


register(
    Tool(
        name="list_positions",
        description=(
            "Individual holdings, largest first, optionally filtered by broker or "
            "asset type. Prefer get_portfolio_summary for totals and weights — "
            "this is for when the specific rows matter."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "broker": {"type": "string", "description": "Only this broker."},
                "asset_type": {
                    "type": "string",
                    "description": "stock, etf, crypto, cash or option.",
                },
                "include_closed": {
                    "type": "boolean",
                    "description": "Include fully-sold positions (default false).",
                },
                "limit": {"type": "integer", "description": f"Max rows (default 50, cap {MAX_ROWS})."},
            },
            "required": [],
        },
        handler=_list_positions,
    )
)


def _get_position(symbol: str) -> dict[str, Any]:
    wanted = symbol.strip().upper()
    positions = [p for p in db.list_positions(include_closed=True) if p.symbol == wanted]
    if not positions:
        raise ToolError(f"No position found for {wanted}.")
    total_quantity = sum(p.quantity for p in positions)
    total_value = sum(p.market_value for p in positions)
    total_cost = sum(p.total_cost for p in positions)
    lots = db.list_tax_lots(symbol=wanted)
    portfolio_total = sum(p.market_value for p in db.list_positions())
    return {
        "symbol": wanted,
        "name": next((p.name for p in positions if p.name), wanted),
        "quantity": total_quantity,
        "market_value": _round(total_value),
        "total_cost": _round(total_cost),
        "average_cost": _round(total_cost / total_quantity, 6) if total_quantity else None,
        "current_price": _round(next((p.current_price for p in positions if p.current_price), 0.0), 6),
        "unrealized_gain": _round(total_value - total_cost),
        "unrealized_gain_pct": _round(((total_value / total_cost - 1) * 100) if total_cost else 0.0, 4),
        "weight_pct": _round((total_value / portfolio_total * 100) if portfolio_total else 0.0, 2),
        "sector": next((p.sector for p in positions if p.sector), None),
        "held_at": [
            {
                "broker": p.broker,
                "quantity": p.quantity,
                "market_value": _round(p.market_value),
                "source": p.source,
            }
            for p in positions
        ],
        "tax_lots": [
            {
                "acquired_at": lot.acquired_at,
                "quantity": lot.quantity,
                "cost_basis": _round(lot.cost_basis, 6),
                "unrealized_gain": _round(lot.unrealized_gain),
                "holding_period": lot.holding_period,
                "days_to_long_term": lot.days_to_long_term,
            }
            for lot in lots
        ],
        "freshness": _freshness(positions),
    }


register(
    Tool(
        name="get_position",
        description=(
            "Everything about one holding by ticker: quantity and value aggregated "
            "across brokers, cost basis, weight, and tax lots with holding periods. "
            "Use this rather than filtering list_positions for a single symbol."
        ),
        input_schema={
            "type": "object",
            "properties": {"symbol": {"type": "string", "description": "Ticker, e.g. AAPL."}},
            "required": ["symbol"],
        },
        handler=_get_position,
    )
)


# ---------------------------------------------------------------------------
# get_realized_gains
# ---------------------------------------------------------------------------


def _get_realized_gains(year: str | None = None) -> dict[str, Any]:
    result = realized.realized_gains(year=str(year) if year else None)
    result["available_years"] = realized.available_years()
    result["basis"] = "FIFO matching on the disposal year; a broker using specific-lot or average cost will disagree."
    return result


register(
    Tool(
        name="get_realized_gains",
        description=(
            "FIFO-matched realised gains and losses, per symbol and in total, "
            "optionally for one tax year. Reports the matching assumption — say it "
            "when quoting, because a broker on specific-lot or average cost will "
            "produce different numbers."
        ),
        input_schema={
            "type": "object",
            "properties": {"year": {"type": "string", "description": "Disposal year, e.g. 2025."}},
            "required": [],
        },
        handler=_get_realized_gains,
    )
)


# ---------------------------------------------------------------------------
# list_transactions
# ---------------------------------------------------------------------------


def _list_transactions(
    symbol: str | None = None,
    action: str | None = None,
    broker: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    capped = max(1, min(int(limit), MAX_ROWS))
    rows = db.list_transactions(
        symbol=symbol.strip().upper() if symbol else None,
        action=action.strip().lower() if action else None,
        broker=broker or None,
        since=since or None,
        until=until or None,
        limit=capped,
    )
    return {
        "returned": len(rows),
        "limit": capped,
        "transactions": [
            {
                "occurred_at": t.occurred_at,
                "symbol": t.symbol,
                "action": t.action,
                "quantity": t.quantity,
                "price": _round(t.price, 6),
                "amount": _round(t.amount),
                "broker": t.broker,
            }
            for t in rows
        ],
    }


register(
    Tool(
        name="list_transactions",
        description=(
            "Ledger entries, newest first, filtered by symbol, action, broker or "
            "date range. For realised P&L use get_realized_gains instead of adding "
            "these up."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Ticker."},
                "action": {"type": "string", "description": "buy, sell, dividend, …"},
                "broker": {"type": "string"},
                "since": {"type": "string", "description": "ISO date, inclusive."},
                "until": {"type": "string", "description": "ISO date, inclusive."},
                "limit": {"type": "integer", "description": f"Max rows (default 50, cap {MAX_ROWS})."},
            },
            "required": [],
        },
        handler=_list_transactions,
    )
)


# ---------------------------------------------------------------------------
# get_price_history
# ---------------------------------------------------------------------------


def _get_price_history(symbol: str, start_date: str | None = None) -> dict[str, Any]:
    wanted = symbol.strip().upper()
    cached = db.get_cached_price_history([wanted], start_date=start_date or None)
    series = cached.get(wanted)
    if not series:
        raise ToolError(
            f"No cached price history for {wanted}. Prices are cached by the refresh "
            "job; a symbol never refreshed has none."
        )
    dates, closes = series["dates"], series["closes"]
    first, last = closes[0], closes[-1]
    return {
        "symbol": wanted,
        "start_date": dates[0],
        "end_date": dates[-1],
        "points": len(dates),
        "first_close": _round(first, 6),
        "last_close": _round(last, 6),
        "change_pct": _round(((last / first - 1) * 100) if first else 0.0, 4),
        "high": _round(max(closes), 6),
        "low": _round(min(closes), 6),
        # Summary first, series second: most questions are answered by the
        # numbers above, and a model that needs the shape can read on.
        "series": [{"date": d, "close": _round(c, 6)} for d, c in zip(dates, closes, strict=True)],
        "note": "Split-adjusted daily closes from Serin's cache, not a live quote.",
    }


register(
    Tool(
        name="get_price_history",
        description=(
            "Cached daily closes for one symbol, with the change, high and low "
            "already computed. Split-adjusted; not a live quote."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Ticker."},
                "start_date": {"type": "string", "description": "ISO date; omit for all cached history."},
            },
            "required": ["symbol"],
        },
        handler=_get_price_history,
    )
)


# ---------------------------------------------------------------------------
# find_data_gaps
# ---------------------------------------------------------------------------


def _find_data_gaps() -> dict[str, Any]:
    result = data_gaps_module.data_gaps()
    return {
        "complete": result.get("complete", False),
        "value_affected": _round(result.get("value_affected")),
        "gaps": result.get("gaps") or [],
        "note": (
            "Gaps make other answers less reliable — a broker with no ledger means "
            "transaction-accurate returns are guessing about those holdings."
        ),
    }


register(
    Tool(
        name="find_data_gaps",
        description=(
            "Known problems with this portfolio's data: brokers with no "
            "transaction history, missing cost basis, unpriced or stale holdings. "
            "Check this before making confident claims about returns."
        ),
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=_find_data_gaps,
    )
)

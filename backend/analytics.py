"""Performance analytics — period returns + indicative NAV series.

A faithful time-weighted return (TWR) needs a transaction log so historical
position weights are known on every dividend/deposit. Serin's current model
stores positions and tax lots but not a full transaction history, so this
module computes an **indicative** return: it back-prices today's basket on
the historical price series and reports the period change. The day-change
number is exact (uses current snapshot vs. previous close).

Limitations to flag in the UI:
    - Period returns assume the user's *current* weights held throughout.
      They don't reflect deposits, sales, or weight drift.
    - Cash positions are treated as flat (1.0) — no money-market yield modelled.

When a real transactions table lands, swap this module for a proper TWR/MWR
implementation; the public functions here are the seams.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

from backend import db
from backend.models import Position
from backend.prices import fetch_price_history


@dataclass
class PeriodReturn:
    period: str
    return_pct: float
    start_value: float
    end_value: float
    start_date: str
    end_date: str


def _parse_iso_date(value: str) -> date:
    return date.fromisoformat(value[:10])


def _today_utc() -> date:
    return datetime.now(UTC).date()


def _nav_series(positions: list[Position], history: dict[str, dict]) -> list[tuple[str, float]]:
    """Reconstruct a daily NAV series from today's basket × historical closes.

    Cash positions contribute their market value flat across the window.
    Positions without history contribute their *current* market value flat,
    so the series stays a meaningful aggregate rather than collapsing.
    """
    cash_value = sum(
        position.market_value
        for position in positions
        if position.asset_type == "cash"
    )

    flat_value = 0.0  # positions with no history → carried flat at market value
    by_date: dict[str, float] = {}

    for position in positions:
        if position.asset_type in ("cash", "option"):
            continue
        series = history.get(position.symbol)
        if not series or not series.get("dates") or not series.get("closes"):
            flat_value += position.market_value
            continue
        multiplier = position.quantity  # for stocks/etf/crypto
        for day, close in zip(series["dates"], series["closes"], strict=False):
            by_date[day] = by_date.get(day, 0.0) + multiplier * float(close)

    if not by_date:
        return []

    days = sorted(by_date)
    return [(day, by_date[day] + cash_value + flat_value) for day in days]


def _period_bounds(today: date) -> dict[str, date]:
    # Week-to-date = since last Monday (Mon = 0). If today is Monday this is today.
    week_start = today.fromordinal(today.toordinal() - today.weekday())
    month_start = today.replace(day=1)
    year_start = today.replace(month=1, day=1)
    one_year = today.replace(year=today.year - 1) if today.year > 1900 else today
    return {
        "1D": today,  # 1D handled separately from current snapshot
        "WTD": week_start,
        "MTD": month_start,
        "YTD": year_start,
        "1Y": one_year,
    }


def _find_at_or_before(series: list[tuple[str, float]], target: date) -> tuple[str, float] | None:
    target_iso = target.isoformat()
    best: tuple[str, float] | None = None
    for day, value in series:
        if day <= target_iso:
            best = (day, value)
        else:
            break
    return best


def _today_change(positions: list[Position]) -> tuple[float, float]:
    """Exact day change from current quotes vs. previous closes.

    Returns (absolute_change, change_pct). When we don't have a previous-close
    on hand (no quote ever refreshed), returns (0.0, 0.0) — the UI should
    surface that as a hint to refresh prices.
    """
    current = sum(p.market_value for p in positions)
    previous = 0.0
    have_any_prev = False
    # We don't store yesterday's price separately — call the quote provider
    # for the previous close. To keep this cheap we approximate using the
    # most recent two history bars at the symbol level.
    history_payload = fetch_price_history(period="1w")
    histories = history_payload.get("history", {})
    for p in positions:
        if p.asset_type in ("cash", "option"):
            previous += p.market_value  # treated flat
            have_any_prev = True
            continue
        series = histories.get(p.symbol) or {}
        closes = series.get("closes") or []
        if len(closes) < 2:
            previous += p.market_value
            continue
        prev_close = float(closes[-2])
        previous += prev_close * p.quantity
        have_any_prev = True
    if not have_any_prev or previous <= 0:
        return 0.0, 0.0
    absolute = current - previous
    pct = (absolute / previous * 100) if previous else 0.0
    return round(absolute, 6), round(pct, 4)


# --- Transaction-aware returns (real TWR + MWR) ------------------------------
#
# Securities-sleeve accounting: the portfolio is the set of priceable
# positions; buys are contributions into the sleeve, sells and dividends are
# distributions out of it. Holdings are replayed per-day from the transactions
# log, so weights are historical fact rather than today's basket projected
# backwards. Cash balances and options are outside the sleeve (documented).


def _xirr(cashflows: list[tuple[date, float]]) -> float | None:
    """Annualized money-weighted return via bisection on NPV. ``None`` when
    the flows can't bracket a root (e.g. all one sign)."""
    if len(cashflows) < 2:
        return None
    t0 = cashflows[0][0]

    def npv(rate: float) -> float:
        total = 0.0
        for day, amount in cashflows:
            years = (day - t0).days / 365.25
            total += amount / ((1.0 + rate) ** years)
        return total

    low, high = -0.9999, 10.0
    npv_low, npv_high = npv(low), npv(high)
    # Short windows produce huge annualized rates; grow the bracket until it
    # straddles the root (discount exponents stay small, so this is stable).
    while npv_low * npv_high > 0 and high < 1e15:
        high *= 10
        npv_high = npv(high)
    if npv_low * npv_high > 0:
        return None
    for _ in range(200):
        mid = (low + high) / 2
        value = npv(mid)
        if abs(value) < 1e-9:
            return mid
        if npv_low * value < 0:
            high = mid
        else:
            low, npv_low = mid, value
    return (low + high) / 2


def transaction_returns(
    positions: list[Position] | None = None,
    history: dict[str, dict] | None = None,
) -> dict:
    """Real TWR + annualized MWR from the transactions log.

    Requires at least one buy/sell transaction; without a log this returns
    ``{"available": False}`` and callers fall back to the indicative numbers.
    """
    positions = positions if positions is not None else db.list_positions()
    if history is None:
        history = fetch_price_history(period="1y").get("history", {})

    transactions = sorted(
        db.list_transactions(limit=100_000),
        key=lambda t: t.occurred_at,
    )
    trades = [t for t in transactions if t.action in ("buy", "sell") and t.symbol]
    if not trades:
        return {"available": False, "reason": "No buy/sell transactions recorded yet."}

    # Priceable sleeve: everything with a close series. Options and cash stay
    # outside; positions without trades are treated as held throughout.
    sleeve = [p for p in positions if p.asset_type not in ("cash", "option")]
    if not sleeve or not history:
        return {"available": False, "reason": "No priced positions with history."}

    # Trading-day grid: the full union of close dates. Positions without
    # trades are held throughout; traded positions are replayed, so a
    # position bought mid-window is simply absent (qty 0) before its buy.
    days = sorted({day for series in history.values() for day in series.get("dates", [])})
    if len(days) < 2:
        return {"available": False, "reason": "Not enough price history."}

    # Replay holdings back to the window start: qty_start = qty_now − Σ deltas
    # of trades inside the window (trades after the last close are excluded —
    # they haven't been valued yet).
    qty_now = {p.symbol: p.quantity for p in sleeve}
    window_trades = [t for t in trades if days[0] <= t.occurred_at[:10] <= days[-1]]
    qty_start = dict(qty_now)
    for trade in window_trades:
        delta = trade.quantity if trade.action == "buy" else -trade.quantity
        qty_start[trade.symbol] = qty_start.get(trade.symbol, 0.0) - delta

    # Close lookup with carry-forward (and first-close backfill).
    closes: dict[str, dict[str, float]] = {}
    for symbol, series in history.items():
        closes[symbol] = dict(zip(series.get("dates", []), series.get("closes", []), strict=False))

    def value_on(day: str, quantities: dict[str, float]) -> float:
        total = 0.0
        for symbol, qty in quantities.items():
            if qty == 0:
                continue
            series = closes.get(symbol)
            if not series:
                # No history at all — carry at current market value.
                position = next((p for p in sleeve if p.symbol == symbol), None)
                if position is not None:
                    total += position.market_value
                continue
            close = series.get(day)
            if close is None:
                prior = [d for d in series if d <= day]
                close = series[max(prior)] if prior else series[min(series)]
            total += qty * float(close)
        return total

    # Per-day contributions (+into sleeve) and dividends (out of sleeve).
    flows_by_day: dict[str, float] = {}
    mwr_flows_by_day: dict[str, float] = {}
    for t in transactions:
        day = t.occurred_at[:10]
        if day < days[0] or day > days[-1]:
            continue
        if t.action == "buy" and t.symbol:
            invested = t.quantity * t.price + t.fee
            flows_by_day[day] = flows_by_day.get(day, 0.0) + invested
            mwr_flows_by_day[day] = mwr_flows_by_day.get(day, 0.0) - invested
        elif t.action == "sell" and t.symbol:
            proceeds = t.quantity * t.price - t.fee
            flows_by_day[day] = flows_by_day.get(day, 0.0) - proceeds
            mwr_flows_by_day[day] = mwr_flows_by_day.get(day, 0.0) + proceeds
        elif t.action == "dividend" and t.symbol:
            amount = abs(t.amount) if t.amount else (t.price or t.quantity)
            flows_by_day[day] = flows_by_day.get(day, 0.0) - amount
            mwr_flows_by_day[day] = mwr_flows_by_day.get(day, 0.0) + amount

    # Walk the grid. The chain starts on the first day the sleeve has value —
    # trades up to and including that day are start capital, not flows (this
    # also handles brand-new portfolios funded entirely inside the window).
    quantities = dict(qty_start)
    trade_idx = 0
    start_idx: int | None = None
    start_value = 0.0
    for index, day in enumerate(days):
        while trade_idx < len(window_trades) and window_trades[trade_idx].occurred_at[:10] <= day:
            t = window_trades[trade_idx]
            delta = t.quantity if t.action == "buy" else -t.quantity
            quantities[t.symbol] = quantities.get(t.symbol, 0.0) + delta
            trade_idx += 1
        value = value_on(day, quantities)
        if value > 0:
            start_idx, start_value = index, value
            break
    if start_idx is None or start_idx >= len(days) - 1:
        return {"available": False, "reason": "Not enough valued history after the first transaction."}
    day_zero = days[start_idx]

    twr_chain = 1.0
    previous_value = start_value
    net_contributions = 0.0
    for day in days[start_idx + 1:]:
        while trade_idx < len(window_trades) and window_trades[trade_idx].occurred_at[:10] <= day:
            t = window_trades[trade_idx]
            delta = t.quantity if t.action == "buy" else -t.quantity
            quantities[t.symbol] = quantities.get(t.symbol, 0.0) + delta
            trade_idx += 1
        value = value_on(day, quantities)
        flow = flows_by_day.get(day, 0.0)
        net_contributions += max(flow, 0.0)
        if previous_value > 0:
            twr_chain *= (value - flow) / previous_value
        previous_value = value

    end_value = previous_value
    twr_pct = (twr_chain - 1) * 100

    years = max((_parse_iso_date(days[-1]) - _parse_iso_date(day_zero)).days, 1) / 365.25
    twr_annualized = ((twr_chain ** (1 / years)) - 1) * 100 if years >= 0.5 and twr_chain > 0 else None

    cashflows: list[tuple[date, float]] = [(_parse_iso_date(day_zero), -start_value)]
    for day in days[start_idx + 1:]:
        flow = mwr_flows_by_day.get(day, 0.0)
        if flow:
            cashflows.append((_parse_iso_date(day), flow))
    cashflows.append((_parse_iso_date(days[-1]), end_value))
    window_days = (_parse_iso_date(days[-1]) - _parse_iso_date(day_zero)).days
    # Annualizing a sub-quarter IRR is misleading; report it only for
    # windows of at least ~a quarter. Modified Dietz covers short windows.
    mwr_annual = _xirr(cashflows) if window_days >= 90 else None

    # Modified Dietz — the standard non-annualized money-weighted period
    # return: (gain − net flows) / time-weighted average capital.
    dietz_denominator = start_value
    net_flow_sum = 0.0
    for day in days[start_idx + 1:]:
        flow = flows_by_day.get(day, 0.0)
        if not flow:
            continue
        net_flow_sum += flow
        weight = (window_days - (_parse_iso_date(day) - _parse_iso_date(day_zero)).days) / window_days if window_days else 0.0
        dietz_denominator += flow * weight
    mwr_period = (
        (end_value - start_value - net_flow_sum) / dietz_denominator
        if dietz_denominator > 0
        else None
    )

    return {
        "available": True,
        "basis": "transactions",
        "start_date": day_zero,
        "end_date": days[-1],
        "start_value": round(start_value, 2),
        "end_value": round(end_value, 2),
        "twr_pct": round(twr_pct, 4),
        "twr_annualized_pct": round(twr_annualized, 4) if twr_annualized is not None else None,
        "mwr_period_pct": round(mwr_period * 100, 4) if mwr_period is not None else None,
        "mwr_annualized_pct": round(mwr_annual * 100, 4) if mwr_annual is not None else None,
        "net_contributions": round(net_contributions, 2),
        "trade_count": len(window_trades),
        "note": (
            "TWR chains daily returns with contributions removed; MWR is the "
            "annualized internal rate of return of your actual cashflows. "
            "Securities only — cash balances and options sit outside this sleeve; "
            "positions without recorded trades are treated as held throughout."
        ),
    }


def period_returns(positions: list[Position] | None = None) -> dict:
    """Compute Today / WTD / MTD / YTD / 1Y / Max period returns.

    Returns a dict with NAV series + per-period returns. NAV is indicative
    (today's weights × historical closes); 1D is exact.
    """
    positions = positions if positions is not None else db.list_positions()
    history_payload = fetch_price_history(period="1y")
    series = _nav_series(positions, history_payload.get("history", {}))

    today = _today_utc()
    bounds = _period_bounds(today)

    results: list[PeriodReturn] = []

    if series:
        latest_day, latest_value = series[-1]
        first_day, first_value = series[0]
        for label, anchor in bounds.items():
            if label == "1D":
                continue
            point = _find_at_or_before(series, anchor)
            if not point:
                continue
            start_day, start_value = point
            if start_value <= 0:
                continue
            ret = (latest_value / start_value - 1) * 100
            results.append(
                PeriodReturn(
                    period=label,
                    return_pct=round(ret, 4),
                    start_value=round(start_value, 6),
                    end_value=round(latest_value, 6),
                    start_date=start_day,
                    end_date=latest_day,
                )
            )
        # Max = first datapoint vs latest
        if first_value > 0 and first_day != latest_day:
            ret = (latest_value / first_value - 1) * 100
            results.append(
                PeriodReturn(
                    period="MAX",
                    return_pct=round(ret, 4),
                    start_value=round(first_value, 6),
                    end_value=round(latest_value, 6),
                    start_date=first_day,
                    end_date=latest_day,
                )
            )

    day_abs, day_pct = _today_change(positions)

    accurate = transaction_returns(positions, history_payload.get("history", {}))

    return {
        "today_change": day_abs,
        "today_change_pct": day_pct,
        "accurate": accurate,
        "periods": [
            {
                "period": r.period,
                "return_pct": r.return_pct,
                "start_value": r.start_value,
                "end_value": r.end_value,
                "start_date": r.start_date,
                "end_date": r.end_date,
            }
            for r in results
        ],
        "nav_series": [
            {"date": day, "value": round(value, 2)} for day, value in series
        ],
        "indicative": True,
        "note": (
            "Period returns are indicative: they back-price today's basket on "
            "historical closes and don't reflect past deposits, sales, or weight "
            "drift. Today's change uses your current snapshot."
        ),
    }


__all__ = ["period_returns", "transaction_returns", "PeriodReturn"]

"""Transaction-accurate portfolio history: daily value, TWR, MWR, coverage.

The distinction this module exists to make:

    portfolio value = market value of open positions + cash balances

with the boundary drawn around the *whole portfolio* rather than around the
invested sleeve. Buying does not make you richer and selling does not make you
poorer — both just move value between cash and securities, so neither is a
flow. Only money crossing the boundary is: deposits and withdrawals.

``backend.analytics.transaction_returns`` draws the boundary differently, at
the sleeve, where a buy reads as a contribution and a sell as a withdrawal.
That answers "how did my invested capital do"; this answers "how did my
portfolio do". Both are legitimate and they disagree, which is why they are
separate functions rather than one with a flag.

History is reconstructed by **rewinding from today** rather than replaying
forward from zero. Today's holdings and today's cash are the one state we
actually know; a forward replay would need a complete ledger back to the
first deposit, which almost nobody has. Rewinding also handles closed
positions naturally: a sold-out holding has quantity zero today, so undoing
its sale puts the shares back.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from backend import db
from backend.models import Transaction, canonical_action

#: Days of history the reconstruction covers, matching the "1y" fetch period.
#: A little over a year so a trade on the window's first day is still priced.
_HISTORY_DAYS = 400

# --- cash ---------------------------------------------------------------


def cash_today(positions: list) -> float:
    """Cash across every broker, in display currency."""
    return sum(p.market_value for p in positions if p.asset_type == "cash")


def cash_delta(transaction: Transaction) -> float:
    """What this row did to the cash balance. Zero for non-cash events."""
    return float(transaction.amount or 0.0)


# --- reconstruction -----------------------------------------------------


def _close_lookup(history: dict[str, dict]) -> dict[str, dict[str, float]]:
    return {
        symbol: dict(zip(series.get("dates", []), series.get("closes", []), strict=False))
        for symbol, series in history.items()
    }


def _close_on(series: dict[str, float], day: str) -> float | None:
    """Last close at or before ``day``; carry forward across non-trading days.

    A missing close is a market holiday, not a vanished position — carrying
    the previous one forward is the difference between a flat weekend and a
    portfolio that appears to be worth nothing every Saturday.
    """
    if not series:
        return None
    direct = series.get(day)
    if direct is not None:
        return float(direct)
    prior = [d for d in series if d <= day]
    if prior:
        return float(series[max(prior)])
    return None


def daily_composition(
    positions: list,
    transactions: list[Transaction],
    history: dict[str, dict],
    splits: dict[str, list[tuple[str, float]]] | None = None,
    conflicts: dict[str, tuple[str, float]] | None = None,
) -> list[dict]:
    """``[{date, cash, securities, values: {symbol: market value}}]`` per day.

    The rewind itself, keeping the per-symbol breakdown it already computes on
    the way to a total. ``daily_values`` is this with the breakdown dropped;
    the X-ray's drift report is this with the totals dropped, because a
    portfolio's shape can change beyond recognition while its value barely
    moves — a year where one holding doubles and the rest go nowhere shows up
    here and almost nowhere else.

    Quantities and cash are rewound from today's known state: for any day D,
    subtract the effect of every transaction after D.

    ``splits`` maps a symbol to ``[(date, ratio)]`` — 10.0 for a 10-for-1 —
    and exists because a trade's share count means different things on either
    side of one. Without it, twelve NVDA bought before the 10:1 are subtracted
    as twelve from a holding of a hundred and twenty, leaving a phantom
    hundred and eight shares standing in every day before the purchase.
    Omitting it is safe but leaves that error in place for pre-split trades.

    Rewinding assumes the ledger and today's holdings agree. When they do not,
    undoing more buys than were ever held drives the reconstructed count below
    zero, and a negative share count priced at a positive close produces a
    negative "value" — days of it, plotted as though it were history. Nobody
    has ever held minus four hundred thousand dollars of stock, so the count
    is floored at zero and the contradiction is recorded rather than drawn:
    pass a dict as ``conflicts`` to collect ``{symbol: (day, dollar impact)}``.
    """
    days = sorted({d for series in history.values() for d in series.get("dates", [])})
    if len(days) < 2:
        return []
    splits = splits or {}

    closes = _close_lookup(history)
    ordered = sorted(transactions, key=lambda t: t.occurred_at[:10])

    # Closed positions are included deliberately: quantity 0 today, non-zero
    # once a sale is undone. Excluding them is precisely the survivor bias.
    qty: dict[str, float] = {}
    for position in positions:
        if position.asset_type in ("cash", "option"):
            continue
        qty[position.symbol] = qty.get(position.symbol, 0.0) + position.quantity

    balance = cash_today(positions)

    # Walk backwards, undoing each day's transactions as we pass it.
    by_day: dict[str, list[Transaction]] = {}
    for t in ordered:
        by_day.setdefault(t.occurred_at[:10], []).append(t)

    # Cumulative split factor per symbol, grown as the walk moves back past
    # each split. Multiplying a historical trade by it restates that trade in
    # today's share units.
    factor: dict[str, float] = {}
    splits_by_day: dict[str, list[tuple[str, float]]] = {}
    for symbol, events in splits.items():
        for when, ratio in events:
            splits_by_day.setdefault(str(when)[:10], []).append((symbol, float(ratio)))

    # symbol -> the latest (i.e. first encountered walking back) day whose
    # rewind went impossible. Everything at or before it is reconstructed from
    # a ledger that contradicts itself. An out-parameter rather than a second
    # return value, so the nine existing call sites keep unpacking one list.
    _conflicts = conflicts if conflicts is not None else {}

    # Peak reconstructed value per symbol, used below to decide which ones
    # matter enough that the series should not begin before they are priced.
    peak: dict[str, float] = {}

    series: list[dict] = []
    for day in reversed(days):
        securities = 0.0
        values: dict[str, float] = {}
        for symbol, quantity in qty.items():
            if not quantity:
                continue
            close = _close_on(closes.get(symbol, {}), day)
            if close is not None:
                value = quantity * close
                securities += value
                values[symbol] = value
                if abs(value) > peak.get(symbol, 0.0):
                    peak[symbol] = abs(value)
        # Unrounded: the rounding that callers see belongs to them, and a
        # weight is a ratio of two of these — rounding both to cents first
        # buys nothing and loses the last digit of a small position's share.
        series.append(
            {"date": day, "securities": securities, "cash": balance, "values": values}
        )
        # Undo this day's rows so the next (earlier) iteration sees the state
        # as it was *before* them.
        #
        # Netted per symbol before being applied, not undone row by row. This
        # series carries one value per day, so the order of fills inside a day
        # is not information it can represent — and applying them singly makes
        # the count dip through states that never existed between the open and
        # the close. Selling 251 SQQQ and buying 250 back the same session
        # rewound through -250 and was recorded as the ledger contradicting
        # the holdings, when the day nets to +1 and nothing is wrong at all.
        day_net: dict[str, float] = {}
        for t in by_day.get(day, []):
            action = canonical_action(t.action)
            if action in ("buy", "sell") and t.symbol:
                # `qty` is carried in today's share units, but a trade records
                # the shares as they were counted on its own day. Across a
                # split those are different units, so a pre-split trade has to
                # be restated before it can be subtracted — see `factor`.
                delta = t.quantity * factor.get(t.symbol, 1.0)
                day_net[t.symbol] = day_net.get(t.symbol, 0.0) + (
                    delta if action == "buy" else -delta
                )
            # Options sit outside this reconstruction: they are excluded from
            # `qty` above because there is no historical price series for a
            # contract. Counting their cash while ignoring their value made
            # buying one look like losing the money — a $21,700 purchase read
            # as a $21,700 fall, and four on one day as -$64,208. Either both
            # sides are in or neither is, and only one of those is available.
            #
            # What this costs: the realized result of an option is missing
            # from portfolio value. It is reported in full on the Realized
            # results panel, which reads the ledger directly.
            if (t.asset_type or "") != "option":
                balance -= cash_delta(t)

        for symbol, net in day_net.items():
            rewound = qty.get(symbol, 0.0) - net
            # A share count below zero is not a small numerical error, it is
            # proof the ledger disagrees with the holdings — a duplicated buy,
            # or a transfer in that never appeared as a purchase. Floor it and
            # record how far it went, so the caller can weigh the
            # contradiction rather than merely notice it.
            if rewound < -1e-9:
                close = _close_on(closes.get(symbol, {}), day) or 0.0
                impact = abs(rewound) * close
                if impact > _conflicts.get(symbol, ("", 0.0))[1]:
                    _conflicts[symbol] = (day, impact)
                rewound = 0.0
            qty[symbol] = rewound
        # A split on this day means every *earlier* day counted shares in
        # smaller units. Applied after the day's own rows, because a split
        # takes effect at the open: a trade dated the same day is already in
        # the new units.
        #
        # Note what is deliberately NOT done here: `qty` itself is never
        # rescaled. The price series is split-adjusted at source (Yahoo's
        # chart closes report NVDA at $121 on the day before its 10:1, not
        # $1,210), so today's share count is already the right multiplier for
        # every historical close. Rewinding the count as well would divide by
        # the split twice.
        for symbol, ratio in splits_by_day.get(day, ()):
            if ratio > 0:
                factor[symbol] = factor.get(symbol, 1.0) * ratio

    series.reverse()
    return _trim_to_priced(series, closes, peak)


def daily_values(
    positions: list,
    transactions: list[Transaction],
    history: dict[str, dict],
    splits: dict[str, list[tuple[str, float]]] | None = None,
    conflicts: dict[str, tuple[str, float]] | None = None,
) -> list[dict]:
    """``[{date, securities, cash, total}]`` for every day we can price."""
    return [
        {
            "date": point["date"],
            "securities": round(point["securities"], 2),
            "cash": round(point["cash"], 2),
            "total": round(point["securities"] + point["cash"], 2),
        }
        for point in daily_composition(positions, transactions, history, splits, conflicts)
    ]


#: A symbol below this share of the portfolio's peak is not worth delaying the
#: whole series for. SPCX listed in June 2026 and is worth a few hundred
#: dollars; waiting for it would discard ten months of history.
_MATERIAL_SHARE = 0.01


def _trim_to_priced(series: list[dict], closes: dict[str, dict[str, float]],
                    peak: dict[str, float]) -> list[dict]:
    """Drop leading days where the portfolio is not yet representable.

    ``days`` is the union of every symbol's price dates, and providers do not
    agree on where a year starts: BTC's series opened 2025-08-30 while all 26
    equities opened 2025-09-02. For three days the reconstruction priced BTC
    and nothing else — securities of $136.98 against a real $411,000 — and
    then the equities arrived at once. Chain-linked, that single day read as a
    100% gain and carried the year's return to +149%.

    Nothing is wrong with those days' arithmetic; they are answering about a
    portfolio that does not exist. So the series starts once every symbol that
    matters has a price.
    """
    if not series or not peak:
        return series
    biggest = max(peak.values(), default=0.0)
    if biggest <= 0:
        return series
    material = [s for s, value in peak.items() if value >= biggest * _MATERIAL_SHARE]
    starts = [min(dates) for symbol in material
              if (dates := closes.get(symbol) or {})]
    if not starts:
        return series
    begin = max(starts)
    trimmed = [point for point in series if point["date"] >= begin]
    # Never trim away so much that there is nothing left to measure.
    return trimmed if len(trimmed) >= 2 else series


#: A contradiction has to be worth this much of the portfolio before it costs
#: the reader their history. One share of SPCX out of place — $161 against
#: $585,370 — is a footnote; discarding eight months of chart over it trades a
#: rounding error for the whole picture, which is the worse mistake.
_MATERIAL_CONFLICT = 0.01          # 1% of the book


def reliable_from(series: list[dict], conflicts: dict[str, tuple[str, float]],
                  portfolio_value: float = 0.0) -> str | None:
    """The earliest date the reconstruction can be shown as measured history.

    Days at or before a *material* conflict are built from a ledger that
    contradicts the holdings; the number drawn there is not a smaller truth,
    it is a different portfolio, so callers window to this date. Immaterial
    ones are still floored — a negative holding is never drawn — but they do
    not cost the reader the rest of the year.
    """
    if not conflicts:
        return None
    floor = (portfolio_value or 0.0) * _MATERIAL_CONFLICT
    material = [day for day, impact in conflicts.values() if impact > floor]
    if not material:
        return None
    after = [p["date"] for p in series if p["date"] > max(material)]
    return after[0] if after else None


# --- returns ------------------------------------------------------------


def _xirr(cashflows: list[tuple[date, float]]) -> float | None:
    """Annualized IRR by bisection. None when the flows do not bracket a root."""
    if len(cashflows) < 2:
        return None
    if not (any(a < 0 for _, a in cashflows) and any(a > 0 for _, a in cashflows)):
        return None
    start = min(d for d, _ in cashflows)

    def npv(rate: float) -> float:
        total = 0.0
        for when, amount in cashflows:
            years = (when - start).days / 365.0
            try:
                total += amount / ((1 + rate) ** years)
            except (OverflowError, ZeroDivisionError):
                return float("inf")
        return total

    low, high = -0.9999, 10.0
    f_low, f_high = npv(low), npv(high)
    if f_low * f_high > 0:
        return None
    for _ in range(200):
        mid = (low + high) / 2
        value = npv(mid)
        if abs(value) < 1e-7:
            return mid
        if f_low * value < 0:
            high, f_high = mid, value
        else:
            low, f_low = mid, value
    return (low + high) / 2


def time_weighted_return(series: list[dict], external_by_day: dict[str, float]) -> float | None:
    """Chain-linked TWR over the daily series, neutralising external flows.

    Each day's factor is (value − flow) / previous value: the flow is removed
    from the ending value so contributing money cannot look like a gain and
    withdrawing it cannot look like a loss.
    """
    if len(series) < 2:
        return None
    chain = 1.0
    previous = series[0]["total"]
    for point in series[1:]:
        flow = external_by_day.get(point["date"], 0.0)
        if previous <= 0:
            # No capital at risk to earn a return on. Re-base rather than
            # divide by zero or invent a percentage from nothing.
            previous = point["total"]
            continue
        chain *= (point["total"] - flow) / previous
        previous = point["total"]
    return (chain - 1) * 100


def money_weighted_return(
    series: list[dict], external_by_day: dict[str, float]
) -> tuple[float | None, float | None]:
    """(period %, annualized %) from dated external flows plus ending value."""
    if len(series) < 2:
        return None, None
    flows: list[tuple[date, float]] = []
    opening = series[0]["total"]
    start = datetime.fromisoformat(series[0]["date"]).date()
    if opening:
        flows.append((start, -opening))
    for point in series[1:]:
        amount = external_by_day.get(point["date"], 0.0)
        if amount:
            flows.append((datetime.fromisoformat(point["date"]).date(), -amount))
    end = datetime.fromisoformat(series[-1]["date"]).date()
    flows.append((end, series[-1]["total"]))

    annual = _xirr(flows)
    contributed = opening + sum(v for v in external_by_day.values() if v > 0)
    withdrawn = -sum(v for v in external_by_day.values() if v < 0)
    period = None
    if contributed:
        period = ((series[-1]["total"] + withdrawn) - contributed) / contributed * 100
    span = (end - start).days
    return period, (annual * 100 if annual is not None and span >= 90 else None)


# --- coverage -----------------------------------------------------------


def coverage(positions: list, transactions: list[Transaction]) -> dict:
    """What the history is actually built from, so the UI can stop implying
    precision it does not have.

    Three questions: when does the record start, is the cash side explained,
    and are there holdings with no trades behind them (which can only be
    back-priced, not reconstructed).
    """
    trades = [t for t in transactions if canonical_action(t.action) in ("buy", "sell")]
    external = [t for t in transactions if canonical_action(t.action) in ("deposit", "withdrawal")]
    traded_symbols = {t.symbol for t in trades if t.symbol}
    held = {p.symbol for p in positions if p.asset_type not in ("cash", "option")}
    untraded = sorted(held - traded_symbols)

    start = min((t.occurred_at[:10] for t in transactions), default="")

    if not transactions:
        quality = "holdings_only"
    elif not trades:
        quality = "holdings_only"
    elif untraded:
        quality = "partial"
    elif not external:
        # Trades but no deposits: the money that bought them came from
        # somewhere unrecorded. Whether or not a cash balance is tracked, the
        # cash side is unexplained, and returns cannot yet tell money added
        # apart from money made.
        quality = "missing_cash_activity"
    else:
        quality = "complete"

    return {
        "quality": quality,
        # True when any number shown is back-priced from today's holdings
        # rather than reconstructed from transactions.
        "estimated": quality != "complete",
        "since": start,
        "transactions": len(transactions),
        "trades": len(trades),
        "external_flows": len(external),
        "symbols_without_trades": untraded,
        "message": _coverage_message(quality, start, untraded),
    }


def _coverage_message(quality: str, start: str, untraded: list[str]) -> str:
    if quality == "complete":
        return f"Transaction history is complete from {start}."
    if quality == "holdings_only":
        return (
            "Only current holdings are known, so performance is estimated by "
            "back-pricing what you hold today. Import an activity or trade "
            "statement to replace it with real returns."
        )
    if quality == "missing_cash_activity":
        return (
            f"Trades are recorded from {start}, but no deposits or withdrawals "
            "are. Returns cannot yet separate money you added from money you made."
        )
    listed = ", ".join(untraded[:4]) + ("…" if len(untraded) > 4 else "")
    return (
        f"History starts {start}. {listed} have no trades on record, so their "
        "contribution is estimated from today's holdings."
    )


# --- the public entry point ---------------------------------------------


#: The ranges the dashboard offers, as days back from the last close. ``None``
#: means the whole series; "ytd" is resolved against the series' own end date.
_RANGE_DAYS = {"1w": 7, "1m": 30, "3m": 91, "ytd": None, "all": None}


def _range_start(series: list[dict], key: str) -> str | None:
    """The cutoff date for one range, or None for the whole series."""
    if key == "all":
        return None
    end = series[-1]["date"]
    if key == "ytd":
        return f"{end[:4]}-01-01"
    days = _RANGE_DAYS.get(key)
    if not days:
        return None
    return (date.fromisoformat(end) - timedelta(days=days)).isoformat()


def _anchor(series: list[dict], cutoff: str | None) -> int:
    """Index of the last point at or before ``cutoff``.

    The previous close, not the first one inside the window. Anchoring on the
    first close of the year measures from January 2nd and silently discards
    the move on the 2nd itself — worth 2.5 points of a year's return in one
    real portfolio, which is how the same period came to read 5.01% in one
    place and 2.5% in another.
    """
    if not cutoff:
        return 0
    prior = [i for i, point in enumerate(series) if point["date"] <= cutoff]
    return prior[-1] if prior else 0


def returns_by_range(series: list[dict],
                     external_by_day: dict[str, float]) -> dict[str, dict]:
    """Time-weighted return per dashboard range.

    TWR is the industry measure and the reason this exists: it breaks the
    series at every external flow so deposits cannot look like gains and
    withdrawals cannot look like losses. A portfolio that earned $22,645 while
    $145,000 was withdrawn is up, and only a raw value change calls that a
    12.62% loss.
    """
    out: dict[str, dict] = {}
    if len(series) < 2:
        return out
    for key in _RANGE_DAYS:
        window = series[_anchor(series, _range_start(series, key)):]
        if len(window) < 2:
            continue
        twr = time_weighted_return(window, external_by_day)
        flows = sum(amount for day, amount in external_by_day.items()
                    if day > window[0]["date"])
        change = window[-1]["total"] - window[0]["total"]
        out[key] = {
            "from": window[0]["date"],
            "to": window[-1]["date"],
            "twr_pct": round(twr, 4) if twr is not None else None,
            "value_change": round(change, 2),
            "net_external": round(flows, 2),
            # What the investments did, with the owner's own transfers taken
            # out — the number the percentage above is a rate for.
            "market_change": round(change - flows, 2),
        }
    return out


def _per_broker(positions: list, transactions: list[Transaction],
                history: dict[str, dict],
                splits: dict[str, list[tuple[str, float]]] | None,
                window: set[str]) -> dict[str, dict]:
    """The same reconstruction, one account at a time.

    daily_values was long documented as unable to be filtered by broker, and
    the chart fell back to pricing today's holdings backwards whenever an
    account was selected. It takes positions and transactions as arguments, so
    it filters perfectly well — the whole-portfolio call is just the case where
    nothing is filtered out.

    The fallback made a real difference: a Robinhood account that had $145,000
    withdrawn from it showed +6.29%, computed from a basket it never held, next
    to a whole-portfolio figure it could not be reconciled with.
    """
    brokers = sorted({p.broker for p in positions if p.broker}
                     | {t.broker for t in transactions if t.broker})
    out: dict[str, dict] = {}
    for broker in brokers:
        held = [p for p in positions if p.broker == broker]
        rows = [t for t in transactions if t.broker == broker]
        if not held and not rows:
            continue
        conflicts: dict[str, tuple[str, float]] = {}
        series = daily_values(held, rows, history, splits, conflicts)
        if len(series) < 2:
            continue
        flows: dict[str, float] = {}
        for t in rows:
            day = t.occurred_at[:10]
            if day in window and canonical_action(t.action) in ("deposit", "withdrawal"):
                flows[day] = flows.get(day, 0.0) + cash_delta(t)
        entry: dict = {
            "series": series,
            "returns": returns_by_range(series, flows),
            "net_external": round(sum(flows.values()), 2),
        }
        # Materiality is judged against this account's own value, not the
        # whole portfolio's: a $200 contradiction is noise in a $900k book and
        # the entire story in a $113 one.
        invested = sum(pos.market_value for pos in held
                       if pos.asset_type != "cash")
        trustworthy = reliable_from(series, conflicts, invested)
        if trustworthy:
            entry["reliable_from"] = trustworthy
            entry["conflicts"] = sorted(conflicts)
        out[broker] = entry
    return out


def _reconstruction_inputs(
    positions: list | None,
    transactions: list[Transaction] | None,
    history: dict[str, dict] | None,
    splits: dict[str, list[tuple[str, float]]] | None,
) -> tuple[list, list[Transaction], dict[str, dict], dict[str, list[tuple[str, float]]]]:
    """Fill in whatever the caller did not supply: holdings, ledger, closes, splits.

    Shared by every public entry point here, so a second reader of the rewind
    cannot end up fetching a subtly different basket than the chart does —
    which would put two numbers on screen that disagree for no visible reason.
    """
    if positions is None:
        positions = db.list_positions(include_closed=True)
    if transactions is None:
        transactions = db.list_transactions(limit=100_000)
    if history is None:
        from backend.prices import fetch_price_history

        # Symbols that were traded but are no longer held still have to be
        # priced: the rewind puts sold shares back, and an unpriced holding
        # contributes nothing to value while its proceeds are rewound in full.
        # That asymmetry turned one AFRM sale into a $49,538 step.
        #
        # Only the ones traded inside the window, though. A ledger reaching
        # back to 2016 names 87 symbols where 21 are still held, and pricing
        # all of them cost 4.8s on a cold cache to fetch closes for holdings
        # that were sold out years before this window opens and cannot appear
        # in it.
        cutoff = (date.today() - timedelta(days=_HISTORY_DAYS)).isoformat()
        traded_in_window = sorted({
            (t.symbol or "").upper() for t in transactions
            if canonical_action(t.action) in ("buy", "sell") and t.symbol
            and (t.occurred_at or "")[:10] >= cutoff
        })
        history = fetch_price_history(
            period="1y", extra_symbols=traded_in_window
        ).get("history", {})
    if splits is None:
        # Only worth a lookup when there are trades to restate — a portfolio
        # entered by hand as holdings has nothing for a split factor to act on.
        traded = {t.symbol for t in transactions if canonical_action(t.action) in ("buy", "sell")}
        if traded:
            from backend import prices

            try:
                splits = prices.fetch_splits(sorted(traded))
            except Exception:
                # Corporate actions are a refinement, not a dependency. Without
                # them the history is wrong only for trades straddling a split
                # — which is where it was before — and that is a far better
                # outcome than an empty dashboard.
                logging.getLogger(__name__).warning(
                    "split lookup failed; history will not restate pre-split trades"
                )
                splits = {}
    return positions, transactions, history, splits or {}


def composition_history(
    positions: list | None = None,
    transactions: list[Transaction] | None = None,
    history: dict[str, dict] | None = None,
    splits: dict[str, list[tuple[str, float]]] | None = None,
) -> dict:
    """The reconstructed portfolio, day by day, with its per-symbol breakdown.

    Same rewind, same coverage verdict and same reliability window as
    ``portfolio_performance`` — this one answers what the portfolio was *made
    of* on each day rather than what it was worth.
    """
    positions, transactions, history, splits = _reconstruction_inputs(
        positions, transactions, history, splits
    )
    cover = coverage(positions, transactions)
    conflicts: dict[str, tuple[str, float]] = {}
    series = daily_composition(positions, transactions, history, splits, conflicts)
    if len(series) < 2:
        return {"available": False, "reason": "Not enough price history.", "coverage": cover}
    invested = sum(p.market_value for p in positions if p.asset_type != "cash")
    return {
        "available": True,
        "series": series,
        "coverage": cover,
        # Days at or before a material contradiction describe a portfolio that
        # never existed. Callers window to this rather than drawing it.
        "reliable_from": reliable_from(series, conflicts, invested),
        "conflicting_symbols": sorted(conflicts),
    }


def portfolio_performance(
    positions: list | None = None,
    transactions: list[Transaction] | None = None,
    history: dict[str, dict] | None = None,
    splits: dict[str, list[tuple[str, float]]] | None = None,
) -> dict:
    """Daily value, TWR, MWR and a coverage verdict for the whole portfolio."""
    positions, transactions, history, splits = _reconstruction_inputs(
        positions, transactions, history, splits
    )
    cover = coverage(positions, transactions)
    conflicts: dict[str, tuple[str, float]] = {}
    series = daily_values(positions, transactions, history, splits, conflicts)
    invested = sum(p.market_value for p in positions if p.asset_type != "cash")
    trustworthy = reliable_from(series, conflicts, invested)
    if len(series) < 2:
        return {"available": False, "reason": "Not enough price history.", "coverage": cover}

    external_by_day: dict[str, float] = {}
    window = {point["date"] for point in series}
    for t in transactions:
        day = t.occurred_at[:10]
        if day in window and canonical_action(t.action) in ("deposit", "withdrawal"):
            external_by_day[day] = external_by_day.get(day, 0.0) + cash_delta(t)

    twr = time_weighted_return(series, external_by_day)
    mwr_period, mwr_annual = money_weighted_return(series, external_by_day)
    net_external = sum(external_by_day.values())

    # A conflict means the ledger and the holdings cannot both be right, so
    # the stretch before it is not a rougher estimate — it is a different
    # portfolio. Published so the chart can start where the reconstruction
    # begins to hold, instead of drawing the contradiction.
    if conflicts:
        cover = dict(cover)
        cover["reliable_from"] = trustworthy
        cover["conflicting_symbols"] = sorted(conflicts)
        cover["conflicts"] = [
            {"symbol": symbol, "date": day, "value": round(impact, 2)}
            for symbol, (day, impact) in sorted(
                conflicts.items(), key=lambda kv: -kv[1][1])
        ]

    return {
        "available": True,
        "coverage": cover,
        "series": series,
        "returns": returns_by_range(series, external_by_day),
        "by_broker": _per_broker(positions, transactions, history, splits, window),
        "start_value": series[0]["total"],
        "end_value": series[-1]["total"],
        "net_external": round(net_external, 2),
        "twr_pct": round(twr, 4) if twr is not None else None,
        "mwr_period_pct": round(mwr_period, 4) if mwr_period is not None else None,
        "mwr_annualized_pct": round(mwr_annual, 4) if mwr_annual is not None else None,
    }

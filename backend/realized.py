"""Realized gains, from the transaction ledger.

The dashboard's gain figure is unrealized only — market value minus cost basis
on what is held *today*. A year of closed round trips is therefore invisible,
which on a real portfolio meant roughly $49,000 of demonstrable gains, plus
dividends and interest, showing up nowhere at all.

The whole difficulty is honesty about what the ledger can prove. A naive FIFO
pass over a partial statement reports every sale as pure gain when its purchase
happens to predate the import: on that same portfolio it claimed $259,205,
of which $210,172 was proceeds whose cost basis simply is not on record. A
number like that is worse than no number, because it looks like an answer.

So sales split into two buckets that are never added together:

* **matched** — a sale FIFO-matched against a recorded purchase. Real gain.
* **unmatched** — a sale with no purchase on record. Proceeds only. Reported
  in shares and dollars so the size of the hole is visible, never as profit.

Income (dividends, interest) and costs (fees, tax) are counted separately
again, because they are certain in a way matched gains are not — they need no
cost basis to be true.
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from typing import Any

from backend import db
from backend.models import canonical_action

logger = logging.getLogger(__name__)

#: Actions that move shares in and out of a lot.
_ACQUIRE = "buy"
_DISPOSE = "sell"

#: Cash events attributed to a symbol but needing no cost basis to be true.
_INCOME = ("dividend", "interest")
_COSTS = ("fee", "tax")

#: Share counts below this are float noise from fractional reinvestment.
_DUST = 1e-9


def _year_of(occurred_at: str) -> str:
    return (occurred_at or "")[:4]


def _contract(txn) -> str:
    """What distinguishes one option contract from another on the same ticker.

    MSFT $430 Call 12/18, $450 Call 8/21 and $465 Call 10/16 are three
    instruments and one symbol. Pooling their lots let a sale of the $450 call
    consume the $430 call's cost basis: the year's total still came out right
    because every lot was eventually used, but two of the three sales were
    reported at -$18,292 and +$25,990 against the broker's +$3,240 and +$4,458.

    The descriptor rides in `notes`, which is where the statement importer puts
    it. Without one an option falls back to pooling by ticker, which is the old
    behaviour and no worse than it was.
    """
    if (txn.asset_type or "") != "option":
        return ""
    return (getattr(txn, "notes", "") or "").strip().upper()


def _walk(year: str | None = None, transactions: list | None = None):
    """Yield one record per ledger event that contributes to a realized figure.

    A generator rather than a second pass, because the drill-in and the
    headline have to agree by construction. Two implementations of FIFO would
    diverge, and a total that does not match the rows under it reads as a bug
    in the numbers rather than in the code.

    Lots are consumed for *every* disposal, in or out of the window: a lot
    sold this year may have been bought years ago, and skipping the earlier
    matching would orphan it. Only reporting is windowed.
    """
    transactions = sorted(
        db.list_transactions(limit=500_000) if transactions is None else transactions,
        key=lambda t: ((t.occurred_at or "")[:10], t.id or 0),
    )
    # Lots are keyed by account as well as symbol, because that is where they
    # live. Selling GOOG at E*Trade consumes an E*Trade lot; it cannot reach
    # into a Robinhood position bought years earlier at a different price.
    # Pooling them by symbol alone let one account's sale claim another's cost
    # basis and report a gain that happened in neither — live rather than
    # theoretical here, with eight symbols traded at two brokers.
    #
    # The cost is that shares transferred between accounts arrive with no
    # purchase and become unmatched proceeds. That is the honest answer: the
    # receiving account genuinely has no record of what they cost, and
    # borrowing a number from the sending one would be a guess wearing the
    # clothes of a fact.
    lots: dict[tuple[str, str], deque[list[float]]] = defaultdict(deque)

    for txn in transactions:
        action = canonical_action(txn.action)
        symbol = (txn.symbol or "").upper()
        # asset_type is part of the key: MSFT shares and MSFT calls are the
        # same ticker and completely different instruments, and matching a
        # call against a share lot would price one in units of the other. So
        # is the contract, for the same reason one strike is not another.
        book = (txn.broker or "", symbol, txn.asset_type or "", _contract(txn))
        in_window = year is None or _year_of(txn.occurred_at) == year

        if action == _ACQUIRE and symbol:
            lots[book].append([float(txn.quantity or 0), float(txn.price or 0)])
            continue

        if action == "transfer" and symbol:
            # Arrived rather than bought — a stock plan vesting into a
            # brokerage account, or an ACAT from another firm. It explains the
            # quantity of a later sale but not its cost, and no earlier
            # statement for *this* account will, because it was never bought
            # here. Yielded so the caller can say which of the two it is.
            if in_window:
                yield {"kind": "arrival", "id": txn.id,
                       "date": (txn.occurred_at or "")[:10], "symbol": symbol,
                       "broker": txn.broker,
                       "quantity": round(abs(float(txn.quantity or 0)), 6)}
            continue

        if action == _DISPOSE and symbol:
            remaining = float(txn.quantity or 0)
            price = float(txn.price or 0)
            # An option contract covers 100 shares and is priced per share, so
            # a $4.23 move on ten contracts is $4,230 and not $42.30. Without
            # this every option result was a hundredth of its real size.
            mult = db.contract_multiplier(txn.asset_type)
            realized = 0.0
            covered = 0.0
            basis = 0.0
            while remaining > _DUST and lots[book]:
                lot_qty, lot_cost = lots[book][0]
                take = min(lot_qty, remaining)
                realized += take * (price - lot_cost) * mult
                basis += take * lot_cost * mult
                covered += take
                remaining -= take
                if lot_qty - take <= _DUST:
                    lots[book].popleft()
                else:
                    lots[book][0][0] = lot_qty - take
            if not in_window:
                continue
            fee = float(txn.fee or 0)
            yield {
                "kind": "disposal",
                "id": txn.id,
                "date": (txn.occurred_at or "")[:10],
                "symbol": symbol,
                "broker": txn.broker,
                "quantity": round(float(txn.quantity or 0), 6),
                "price": round(price, 4),
                "fee": round(fee, 2),
                # The fee belongs to the part that has a cost basis; charging
                # it against unmatched proceeds would imply a gain figure for
                # shares that have none.
                "realized": round(realized - fee, 2) if covered > _DUST else 0.0,
                "matched_shares": round(covered, 6),
                "cost_basis": round(basis, 2),
                "proceeds": round(covered * price * mult, 2),
                "unmatched_shares": round(remaining, 6) if remaining > _DUST else 0.0,
                "unmatched_proceeds": (round(remaining * price * mult, 2)
                                       if remaining > _DUST else 0.0),
            }
            continue

        if not in_window:
            continue
        if action in _INCOME:
            yield {"kind": "income", "id": txn.id, "date": (txn.occurred_at or "")[:10],
                   "symbol": symbol, "broker": txn.broker, "action": action,
                   "amount": round(float(txn.amount or 0), 2)}
        elif action in _COSTS:
            yield {"kind": "cost", "id": txn.id, "date": (txn.occurred_at or "")[:10],
                   "symbol": symbol, "broker": txn.broker, "action": action,
                   "amount": round(abs(float(txn.amount or 0)), 2)}


#: Which events back each figure on the Realized results panel. "net" is the
#: arithmetic of the other three, so it draws from all of them.
_DRIVERS = {
    "net": ("disposal", "income", "cost"),
    "gains": ("disposal",),
    "income": ("income",),
    "costs": ("cost",),
}


def realized_detail(kind: str, year: str | None = None) -> dict[str, Any]:
    """The individual events behind one headline figure, largest first.

    Exists because a summary card invites the question "which trades?" and
    leaves nowhere to ask it. Sums to the same total the card shows, because
    both walk the same lots.
    """
    kinds = _DRIVERS.get(kind)
    if not kinds:
        return {"kind": kind, "year": year, "lines": [], "total": 0.0}

    lines = [event for event in _walk(year) if event["kind"] in kinds]
    if kind == "gains":
        # A disposal with no matched shares contributed nothing to this
        # figure; listing it here would imply it did.
        lines = [line for line in lines if line["matched_shares"] > _DUST]

    def contribution(line: dict) -> float:
        if line["kind"] == "disposal":
            return line["realized"]
        return line["amount"] if line["kind"] == "income" else -line["amount"]

    for line in lines:
        line["contribution"] = round(contribution(line), 2)
    lines.sort(key=lambda line: (-abs(line["contribution"]), line["date"]))

    return {
        "kind": kind,
        "year": year,
        "lines": lines,
        "total": round(sum(line["contribution"] for line in lines), 2),
        "count": len(lines),
    }


def realized_gains(year: str | None = None) -> dict[str, Any]:
    """FIFO-matched realized results, per symbol and in total.

    ``year`` filters on the year of the *disposal* — which is what a tax year
    means — while purchases are matched from the whole ledger regardless of
    when they happened. Restricting the buys too would orphan every sale whose
    lot was bought in December.

    FIFO is assumed. It is the US default, but a broker set to specific-lot or
    average-cost will disagree, and this makes no attempt to read which one was
    actually used.
    """
    matched: dict[str, float] = defaultdict(float)
    unmatched_qty: dict[str, float] = defaultdict(float)
    unmatched_proceeds: dict[str, float] = defaultdict(float)
    income: dict[str, float] = defaultdict(float)
    costs: dict[str, float] = defaultdict(float)
    sale_count: dict[str, int] = defaultdict(int)
    symbols: set[str] = set()

    # Read once and share it with the walk: two full reads of a 500k-row
    # ledger to answer one panel is a waste, and a walk over different rows
    # than the ones the coverage line describes could disagree with it.
    ledger = db.list_transactions(limit=500_000)
    ledger_starts = min((t.occurred_at[:10] for t in ledger if t.occurred_at),
                        default="")

    starts: dict[str, str] = {}
    for t in ledger:
        day = (t.occurred_at or "")[:10]
        if day and (t.broker not in starts or day < starts[t.broker]):
            starts[t.broker] = day
    uncovered: dict[str, dict] = {}

    arrivals: dict[str, float] = defaultdict(float)
    for event in _walk(year, ledger):
        symbol = event["symbol"]
        if event["kind"] == "arrival":
            arrivals[event["broker"] or ""] += event["quantity"]
            continue
        if event["kind"] == "disposal" and event["unmatched_shares"] > _DUST:
            entry = uncovered.setdefault(
                event["broker"] or "",
                {"broker": event["broker"] or "", "since": starts.get(event["broker"], ""),
                 "symbols": set(), "shares": 0.0, "proceeds": 0.0})
            entry["symbols"].add(symbol)
            entry["shares"] += event["unmatched_shares"]
            entry["proceeds"] += event["unmatched_proceeds"]
        if event["kind"] == "disposal":
            symbols.add(symbol)
            sale_count[symbol] += 1
            if event["matched_shares"] > _DUST:
                matched[symbol] += event["realized"]
            if event["unmatched_shares"] > _DUST:
                unmatched_qty[symbol] += event["unmatched_shares"]
                unmatched_proceeds[symbol] += event["unmatched_proceeds"]
        elif event["kind"] == "income":
            income[symbol or ""] += event["amount"]
            if symbol:
                symbols.add(symbol)
        else:
            costs[symbol or ""] += event["amount"]
            if symbol:
                symbols.add(symbol)

    rows = []
    for symbol in sorted(symbols | {s for s in income} | {s for s in costs}):
        row = {
            "symbol": symbol,
            "realized": round(matched.get(symbol, 0.0), 2),
            "unmatched_shares": round(unmatched_qty.get(symbol, 0.0), 6),
            "unmatched_proceeds": round(unmatched_proceeds.get(symbol, 0.0), 2),
            "income": round(income.get(symbol, 0.0), 2),
            "costs": round(costs.get(symbol, 0.0), 2),
            "sales": sale_count.get(symbol, 0),
        }
        if any(row[k] for k in ("realized", "unmatched_proceeds", "income", "costs")):
            rows.append(row)

    rows.sort(key=lambda r: -abs(r["realized"] or r["unmatched_proceeds"]))
    totals = {
        "realized": round(sum(r["realized"] for r in rows), 2),
        "unmatched_shares": round(sum(r["unmatched_shares"] for r in rows), 6),
        "unmatched_proceeds": round(sum(r["unmatched_proceeds"] for r in rows), 2),
        "income": round(sum(r["income"] for r in rows), 2),
        "costs": round(sum(r["costs"] for r in rows), 2),
        "sales": sum(r["sales"] for r in rows),
    }
    # Deliberately excludes unmatched proceeds. That is the whole point of
    # separating them: they are not known to be profit.
    totals["net"] = round(totals["realized"] + totals["income"] - totals["costs"], 2)

    # Split each account's hole: the part a transfer accounts for, and the
    # part that predates its ledger. They need different things done, and one
    # sentence covering both wastes half the effort it asks for.
    uncovered_by_broker = []
    for entry in uncovered.values():
        pool = arrivals.get(entry["broker"], 0.0)
        covered = min(entry["shares"], pool)
        share = covered / entry["shares"] if entry["shares"] else 0.0
        uncovered_by_broker.append({
            "broker": entry["broker"],
            "since": entry["since"],
            "symbols": sorted(entry["symbols"]),
            "shares": round(entry["shares"], 6),
            "proceeds": round(entry["proceeds"], 2),
            "transferred_shares": round(covered, 6),
            "transferred_proceeds": round(entry["proceeds"] * share, 2),
            "older_shares": round(entry["shares"] - covered, 6),
            "older_proceeds": round(entry["proceeds"] * (1 - share), 2),
        })
    uncovered_by_broker.sort(key=lambda e: -e["proceeds"])

    return {
        "year": year,
        "by_symbol": rows,
        "totals": totals,
        "coverage": {
            "ledger_starts": ledger_starts,
            "complete": not totals["unmatched_shares"],
            "symbols_missing_basis": [
                r["symbol"] for r in rows if r["unmatched_shares"] > _DUST
            ],
            # Per account, because one global date is wrong for every broker
            # but the earliest. E*Trade's feed opens in 2024 and Robinhood's in
            # 2016; telling someone their E*Trade shares predate 2016-06-09
            # sends them looking for paperwork that would not close the gap.
            "by_broker": uncovered_by_broker,
        },
        "basis": "fifo",
    }


def available_years() -> list[str]:
    """Years the ledger actually contains disposals or income for."""
    years = set()
    for txn in db.list_transactions(limit=500_000):
        action = canonical_action(txn.action)
        if action == _DISPOSE or action in _INCOME or action in _COSTS:
            year = _year_of(txn.occurred_at)
            if year:
                years.add(year)
    return sorted(years, reverse=True)

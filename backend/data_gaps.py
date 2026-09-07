"""What is missing from the data, and what the person should do about it.

Serin can already say a number is estimated. It could not say *why*, or what
would fix it, so the honest labels became noise: "AAPL, BTC, CRML, FBALX… have
no trades on record" tells you something is wrong and leaves you to work out
that the remedy is a button on another tab.

Every gap here therefore carries an action. A caveat a reader cannot act on is
just an apology.

Ordered by what it costs to leave alone, not by how easy it is to detect.
"""

from __future__ import annotations

import json
from typing import Any

from backend import db
from backend.models import canonical_action

#: Sources that mean a broker connection wrote the row itself.
_SYNCED = frozenset({"snaptrade", "coinbase"})


def _positions_by_broker(positions: list) -> dict[str, list]:
    grouped: dict[str, list] = {}
    for position in positions:
        if position.asset_type not in ("cash", "option"):
            grouped.setdefault(position.broker, []).append(position)
    return grouped


def data_gaps() -> dict[str, Any]:
    """Actionable gaps, worst first.

    Deliberately says nothing when there is nothing to do: a permanent banner
    explaining that everything is fine trains people to ignore the banner on
    the day it matters.
    """
    positions = db.list_positions(include_closed=True)
    transactions = db.list_transactions(limit=500_000)
    gaps: list[dict[str, Any]] = []

    invested = [p for p in positions if p.asset_type not in ("cash", "option")]
    if not invested:
        return {"gaps": [], "complete": True, "value_affected": 0.0}

    traded = {t.symbol for t in transactions
              if canonical_action(t.action) in ("buy", "sell") and t.symbol}
    by_broker = _positions_by_broker(positions)

    # 1. A whole broker with no ledger. The largest and most fixable gap: its
    #    holdings are assumed to have been held all period, so every figure
    #    that claims to be transaction-accurate is guessing about them.
    for broker, held in sorted(by_broker.items()):
        untraded = [p for p in held if p.symbol not in traded]
        if len(untraded) != len(held):
            continue                      # some history exists; not this gap
        value = sum(p.market_value for p in untraded)
        synced = any(p.source in _SYNCED for p in held)
        gaps.append({
            "code": "broker_without_ledger",
            "severity": "high",
            "broker": broker,
            "symbols": sorted(p.symbol for p in untraded),
            "value": round(value, 2),
            "title": f"No trade history for {broker}",
            "detail": (
                f"{len(untraded)} holding{'' if len(untraded) == 1 else 's'} worth "
                f"${value:,.0f} are assumed to have been held all period, so returns "
                "covering them are estimated rather than measured."
                if len(untraded) != 1 else
                f"1 holding worth ${value:,.0f} is assumed to have been held all period, "
                "so returns covering it are estimated rather than measured."
            ),
            "action": ("Import transaction history" if synced
                       else "Import a statement for this account"),
            "action_hint": ("Brokerages → Import transaction history" if synced
                            else "Smart Import → upload the broker's activity export"),
        })

    # 2. Sales with no purchase on record. Distinct from the above: the ledger
    #    reaches this symbol, it just does not reach far enough back, so the
    #    proceeds cannot be turned into a gain.
    # Keyed by account, matching backend.realized: a lot lives in the account
    # that bought it, so a sale in one cannot be covered by a purchase in
    # another. Anything else understates this gap by exactly the amount it
    # overstates realized gains.
    #: All four keyed alike: (broker, symbol, asset_type, contract).
    lots: dict[tuple, float] = {}
    arrived: dict[tuple, float] = {}
    arrivals: dict[tuple[str, str, str], float] = {}
    orphan_qty: dict[tuple, float] = {}
    orphan_value: dict[tuple, float] = {}
    for txn in sorted(transactions, key=lambda t: (t.occurred_at[:10], t.id or 0)):
        action = canonical_action(txn.action)
        if not txn.symbol:
            continue
        # Same key as backend.realized, contract included: the two have to
        # agree or the panel contradicts the figure it is explaining.
        book = (txn.broker, txn.symbol, txn.asset_type or "",
                (txn.notes or "").strip().upper()
                if (txn.asset_type or "") == "option" else "")
        if action == "transfer":
            # Shares that arrived from somewhere else — an RSU or ESPP plan
            # vesting into a brokerage account, or an ACAT from another firm.
            # They carry no price, so they cover the *quantity* of a later sale
            # without explaining its cost. Tracked separately because the fix
            # is different: no earlier statement for this account will contain
            # them, since they were never bought here.
            arrived[book] = arrived.get(book, 0.0) + abs(txn.quantity)
            # Kept per vest date, not just totalled: an RSU lot is worth what
            # it was worth the day it vested, so one price for a decade of
            # allocations would be a fiction. Same-day rows share a price and
            # are collapsed, which turns 46 INTU rows into a handful.
            day = (txn.occurred_at or "")[:10]
            arrivals.setdefault((txn.broker, txn.symbol, day), 0.0)
            arrivals[(txn.broker, txn.symbol, day)] += abs(txn.quantity)
            continue
        if action == "buy":
            lots[book] = lots.get(book, 0.0) + txn.quantity
        elif action == "sell":
            available = lots.get(book, 0.0)
            matched = min(available, txn.quantity)
            lots[book] = available - matched
            missing = txn.quantity - matched
            if missing > 1e-9:
                # The same key the lots and arrivals use. Two shapes of key
                # for one concept is how the transfer split silently stopped
                # matching: `arrived` was filed under one and looked up under
                # the other, so every transferred share read as missing
                # history again.
                orphan_qty[book] = orphan_qty.get(book, 0.0) + missing
                orphan_value[book] = orphan_value.get(book, 0.0) + missing * txn.price

    # Split each hole into the part a transfer explains and the part that
    # predates the ledger. They need different things done about them, and one
    # message covering both sends half the reader's effort in a useless
    # direction: no E*Trade statement from before 2024-09-30 will contain
    # shares that vested in a stock plan account and were moved across.
    transferred: dict[tuple, float] = {}
    for key in list(orphan_qty):
        covered = min(orphan_qty[key], arrived.get(key, 0.0))
        if covered <= 1e-9:
            continue
        share = covered / orphan_qty[key]
        transferred[key] = round(orphan_value[key] * share, 2)
        orphan_value[key] -= transferred[key]
        orphan_qty[key] -= covered
        if orphan_qty[key] <= 1e-9:
            del orphan_qty[key]
            del orphan_value[key]

    by_transfer: dict[str, list[tuple[str, str]]] = {}
    for key in transferred:
        by_transfer.setdefault(key[0], []).append(key)
    for broker, keys in sorted(by_transfer.items(),
                               key=lambda kv: -sum(transferred[k] for k in kv[1])):
        symbols = sorted({k[1] for k in keys})
        total = sum(transferred[k] for k in keys)
        # The rows themselves, so the fix can be offered as a part-filled form
        # rather than as a tab to go and search. Everything except the price is
        # already known.
        lots = sorted(
            ({"symbol": key[1], "broker": key[0], "date": key[2],
              "quantity": round(qty, 6)}
             for key, qty in arrivals.items()
             if key[0] == broker and key[1] in symbols),
            key=lambda lot: (lot["symbol"], lot["date"]),
        )
        gaps.append({
            "code": "transferred_without_cost",
            "lots": lots,
            "severity": "high",
            "broker": broker,
            "symbols": symbols,
            "value": round(total, 2),
            "title": f"Shares moved into {broker} with no cost recorded",
            "detail": (
                f"${total:,.0f} of {', '.join(symbols)} proceeds came from shares "
                f"transferred into this account rather than bought in it — a stock "
                f"plan vesting, or a transfer from another firm. The shares are on "
                f"record; what they cost is not, so the sale cannot be turned into "
                f"a gain."
            ),
            "action": "Record what these shares cost",
            "action_hint": ("Smart Import → upload the stock-plan or sending "
                            "account's history, or add the cost by hand"),
        })

    # One gap per account, dated to that account's own ledger.
    #
    # A single global "history begins on 2016-06-09" is wrong for every other
    # broker and sends the reader hunting for statements they already have.
    # E*Trade's activity feed reaches back to 2024-09-30; its uncovered sales
    # need statements from before *that*, and the Robinhood date says nothing
    # about them.
    starts: dict[str, str] = {}
    for txn in transactions:
        day = (txn.occurred_at or "")[:10]
        if day and (txn.broker not in starts or day < starts[txn.broker]):
            starts[txn.broker] = day

    by_broker: dict[str, list[tuple[str, str]]] = {}
    for key in orphan_qty:
        by_broker.setdefault(key[0], []).append(key)

    for broker, keys in sorted(by_broker.items(),
                               key=lambda kv: -sum(orphan_value[k] for k in kv[1])):
        symbols = sorted({k[1] for k in keys})
        total = sum(orphan_value[k] for k in keys)
        earliest = starts.get(broker, "")
        gaps.append({
            "code": "sales_without_purchase",
            "severity": "high",
            "broker": broker,
            "symbols": symbols,
            "value": round(total, 2),
            "since": earliest,
            "title": f"Some {broker} sales have no purchase on record",
            "detail": (
                f"${total:,.0f} of proceeds across {', '.join(symbols)} cannot "
                f"be turned into gains, because those shares were bought before "
                f"your {broker} history begins on {earliest}."
            ),
            "action": f"Import {broker} statements from before {earliest}",
            "action_hint": "Smart Import → upload an earlier activity export",
        })

    # 3. Holdings no price provider covers. Not fixable by importing anything,
    #    so it is stated rather than actioned — and saying so is the point: a
    #    blank period card with no explanation reads as a broken app.
    from backend import prices

    try:
        history = (prices.fetch_price_history("1y") or {}).get("history") or {}
    except Exception:
        history = {}
    unpriced = sorted({p.symbol for p in invested
                       if not (history.get(p.symbol) or {}).get("dates")})
    if unpriced:
        value = sum(p.market_value for p in invested if p.symbol in unpriced)
        gaps.append({
            "code": "no_price_history",
            "severity": "info",
            "symbols": unpriced,
            "value": round(value, 2),
            "title": "No price history for some holdings",
            "detail": (
                f"{', '.join(unpriced)} "
                f"{'has' if len(unpriced) == 1 else 'have'} no daily closes from the "
                "market-data provider — mutual funds and private placements often do "
                f"not. {'It is' if len(unpriced) == 1 else 'They are'} excluded from "
                "charts and period returns."
            ),
            "action": "",
            "action_hint": "Nothing to do — the data does not exist upstream.",
        })

    # A stable identity per gap, so a dismissal survives a refresh and follows
    # the person between their phone and their desk. Built from what the gap
    # *is* rather than its position in the list: "the etrade transfer gap"
    # stays the same item as its value moves.
    for gap in gaps:
        gap["id"] = ":".join(
            part for part in (gap["code"], gap.get("broker") or "") if part)

    put_away = dismissed_ids()
    for gap in gaps:
        gap["dismissed"] = gap["id"] in put_away

    active = [gap for gap in gaps if not gap["dismissed"]]
    return {
        "gaps": gaps,
        "active": active,
        "dismissed_count": len(gaps) - len(active),
        # "complete" drives whether the panel appears at all, so it answers
        # about the gaps still asking to be seen — not about the ones already
        # filed away in the action hub.
        "complete": not active,
        "value_affected": round(
            sum(g["value"] for g in active if g["severity"] == "high"), 2),
        "value_dismissed": round(
            sum(g["value"] for g in gaps
                if g["dismissed"] and g["severity"] == "high"), 2),
    }


#: Where dismissals live. Server-side rather than in the browser: the same
#: person reads this on a phone and acts on it at a desk, and a reminder that
#: comes back on the other device has not been dismissed, only hidden.
_DISMISSED_KEY = "data_gaps.dismissed"


def dismissed_ids() -> set[str]:
    raw = db.get_setting(_DISMISSED_KEY, "")
    if not raw:
        return set()
    try:
        stored = json.loads(raw)
    except ValueError:
        return set()
    return {str(item) for item in stored} if isinstance(stored, list) else set()


def set_dismissed(gap_id: str, dismissed: bool) -> set[str]:
    """Put one gap away, or bring it back. Returns the new set."""
    current = dismissed_ids()
    if dismissed:
        current.add(gap_id)
    else:
        current.discard(gap_id)
    db.set_setting(_DISMISSED_KEY, json.dumps(sorted(current)))
    return current

from __future__ import annotations

import re
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from backend import db
from backend.models import Position, TaxLot, utcnow_iso

STALE_PRICE_DAYS = 3
HIGH_CASH_THRESHOLD = 0.5
OPTION_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.]*-\d{6}-[CP]\d+(?:\.\d+)?$")

SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}
SEVERITIES = ("critical", "warning", "info")


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _issue(
    *,
    code: str,
    severity: str,
    category: str,
    title: str,
    description: str,
    suggested_action: str,
    position: Position | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    subject = f"{position.symbol}:{position.broker}:{position.asset_type}" if position else "portfolio"
    return {
        "id": f"{code}:{subject}",
        "code": code,
        "severity": severity,
        "category": category,
        "title": title,
        "description": description,
        "suggested_action": suggested_action,
        "symbol": position.symbol if position else "",
        "broker": position.broker if position else "",
        "asset_type": position.asset_type if position else "",
        "evidence": evidence or {},
    }


def _tax_lots_by_position(lots: list[TaxLot]) -> dict[tuple[str, str], list[TaxLot]]:
    grouped: dict[tuple[str, str], list[TaxLot]] = defaultdict(list)
    for lot in lots:
        grouped[(lot.symbol, lot.broker)].append(lot)
    return grouped


#: Position sources that come from a brokerage connection rather than a human.
#: A row carrying one of these was last confirmed by the broker itself.
_SYNCED_SOURCES = frozenset({"snaptrade", "coinbase"})


def audit_portfolio() -> dict[str, Any]:
    """Run deterministic portfolio data checks.

    This intentionally does not call an AI model. It is the source-of-truth
    rule engine; AI can later summarize or explain these exact findings.
    """
    positions = db.list_positions()
    lots = db.list_tax_lots()
    lots_by_position = _tax_lots_by_position(lots)
    now = datetime.now(UTC)
    total_value = sum(position.market_value for position in positions)
    cash_value = sum(position.market_value for position in positions if position.asset_type == "cash")
    issues: list[dict[str, Any]] = []

    for position in positions:
        if position.asset_type == "cash":
            continue

        if position.current_price <= 0:
            issues.append(_issue(
                code="missing_price",
                severity="critical",
                category="pricing",
                title=f"{position.symbol} has no usable price",
                description="Market value and gain/loss calculations are unreliable because current price is zero.",
                suggested_action="Refresh prices or edit the position with a valid current price.",
                position=position,
                evidence={"current_price": position.current_price, "market_value": position.market_value},
            ))

        if position.average_cost <= 0 and position.market_value > 0:
            issues.append(_issue(
                code="missing_cost_basis",
                severity="critical",
                category="cost_basis",
                title=f"{position.symbol} has missing cost basis",
                description="Unrealized gain is overstated or incomplete because average cost is zero.",
                suggested_action="Add the actual average cost or import tax lots for this position.",
                position=position,
                evidence={"average_cost": position.average_cost, "unrealized_gain": position.unrealized_gain},
            ))

        if position.asset_type == "stock" and not position.sector.strip():
            issues.append(_issue(
                code="missing_sector",
                severity="warning",
                category="classification",
                title=f"{position.symbol} sector enrichment is pending",
                description="Sector allocation is less useful while this stock is grouped under Unknown.",
                suggested_action="Refresh market data so Serin can pull the sector from the market-data provider.",
                position=position,
                evidence={"sector": position.sector or "Unknown"},
            ))

        updated_at = _parse_iso(position.updated_at)
        if updated_at:
            age_days = (now - updated_at).total_seconds() / 86400
            if age_days > STALE_PRICE_DAYS:
                issues.append(_issue(
                    code="stale_price",
                    severity="warning",
                    category="pricing",
                    title=f"{position.symbol} price is stale",
                    description=f"This holding has not been updated for {age_days:.1f} days.",
                    suggested_action="Refresh prices before relying on market value or gain/loss.",
                    position=position,
                    evidence={"updated_at": position.updated_at, "age_days": round(age_days, 1)},
                ))

        if position.asset_type == "option" and not OPTION_SYMBOL_RE.match(position.symbol):
            issues.append(_issue(
                code="option_symbol_format",
                severity="warning",
                category="classification",
                title=f"{position.symbol} option symbol may be unparseable",
                description="Serin expects option symbols like MSFT-261218-C430 so expiry, side, and strike can be understood.",
                suggested_action="Edit the option symbol into SYMBOL-YYMMDD-C/PSTRIKE format if this is an option contract.",
                position=position,
                evidence={"symbol": position.symbol},
            ))

        matching_lots = lots_by_position.get((position.symbol, position.broker), [])
        if matching_lots:
            lot_quantity = sum(lot.quantity for lot in matching_lots)
            tolerance = max(0.0001, abs(position.quantity) * 0.001)
            if abs(lot_quantity - position.quantity) > tolerance:
                issues.append(_issue(
                    code="tax_lot_quantity_mismatch",
                    severity="warning",
                    category="tax_lots",
                    title=f"{position.symbol} tax lots do not match position quantity",
                    description="Tax lot quantity differs from the current position quantity.",
                    suggested_action="Add missing tax lots or remove stale lots so tax analysis matches holdings.",
                    position=position,
                    evidence={
                        "position_quantity": position.quantity,
                        "tax_lot_quantity": round(lot_quantity, 8),
                        "lot_count": len(matching_lots),
                    },
                ))

    # A holding the brokerage does not report. Sync only removes rows it wrote
    # itself — deleting somebody's hand-entered data because a vendor did not
    # mention it would be far worse — so a position typed in or imported before
    # the broker was connected survives the sale that closed it, and quietly
    # keeps counting toward net worth.
    #
    # A broker is treated as covered when it has at least one synced position:
    # that is local, needs no network call, and is exactly the evidence that
    # the connection is live and reporting for that broker.
    synced_brokers = {
        position.broker for position in positions if position.source in _SYNCED_SOURCES
    }
    for position in positions:
        if (
            position.asset_type != "cash"
            and position.source not in _SYNCED_SOURCES
            and position.broker in synced_brokers
        ):
            issues.append(_issue(
                code="unconfirmed_by_broker",
                severity="critical",
                category="reconciliation",
                title=f"{position.symbol} is not in your {position.broker} account",
                description=(
                    f"This holding was entered by hand or imported, and the connected "
                    f"{position.broker} account does not report it. If it was sold, its "
                    f"{position.market_value:,.2f} is still counting toward your total."
                ),
                suggested_action=(
                    "Check the position against your broker. If it is closed, delete it — "
                    "the sale is already in your transactions if you imported a statement."
                ),
                position=position,
                evidence={
                    "source": position.source,
                    "quantity": position.quantity,
                    "market_value": round(position.market_value, 2),
                    "broker_is_synced": True,
                },
            ))

    # A quantity the broker disagreed with. Positions are unique on
    # (symbol, broker, asset_type), so the sync overwrote the figure and there
    # is no second row left to compare — the sync records what it replaced on
    # its way past, and this is where that surfaces.
    for conflict in db.sync_conflicts():
        entered = float(conflict.get("entered_quantity") or 0)
        synced = float(conflict.get("synced_quantity") or 0)
        issues.append(_issue(
            code="quantity_overwritten_by_sync",
            severity="warning",
            category="reconciliation",
            title=f"{conflict.get('symbol', '?')} quantity disagreed with your broker",
            description=(
                f"You had {entered:,.4f} shares recorded; the connected "
                f"{conflict.get('broker', 'broker')} account reported {synced:,.4f}. "
                "The broker's figure is now in place."
            ),
            suggested_action=(
                "If the broker is right, nothing to do. If you were tracking a "
                "holding it does not cover, re-enter it under a different broker "
                "so the sync stops overwriting it."
            ),
            evidence={
                "symbol": conflict.get("symbol", ""),
                "broker": conflict.get("broker", ""),
                "entered_quantity": entered,
                "entered_source": conflict.get("entered_source", ""),
                "synced_quantity": synced,
                "difference": round(synced - entered, 8),
            },
        ))

    # The same purchase recorded twice — typed in once and imported once, or
    # imported from two overlapping statements. Tax lots carry no uniqueness
    # constraint by design (buying the same stock twice on one day is
    # ordinary), so nothing stops a genuine double-entry either.
    lots_by_purchase: dict[tuple[str, str, str], list[TaxLot]] = defaultdict(list)
    for lot in lots:
        lots_by_purchase[(lot.symbol, lot.broker, (lot.acquired_at or "")[:10])].append(lot)
    for (symbol, broker, acquired), same_day in lots_by_purchase.items():
        if len(same_day) < 2 or not acquired:
            continue
        quantities = [round(lot.quantity, 6) for lot in same_day]
        # Two fills of different sizes on one day are two real purchases.
        # Identical size and identical cost is what a double-entry looks like.
        costs = [round(lot.cost_basis, 2) for lot in same_day]
        if len(set(zip(quantities, costs, strict=True))) == len(same_day):
            continue
        issues.append(_issue(
            code="duplicate_tax_lot",
            severity="warning",
            category="reconciliation",
            title=f"{symbol} has the same purchase recorded more than once",
            description=(
                f"{len(same_day)} lots of {symbol} at {broker} share the purchase date "
                f"{acquired} with matching quantity and cost. One purchase entered twice "
                "overstates the position and its cost basis."
            ),
            suggested_action="Compare against your broker and delete the duplicate lot.",
            evidence={
                "symbol": symbol, "broker": broker, "acquired_at": acquired,
                "lot_count": len(same_day), "quantities": quantities, "cost_basis": costs,
            },
        ))

    symbol_brokers: dict[tuple[str, str], set[str]] = defaultdict(set)
    for position in positions:
        if position.asset_type != "cash":
            symbol_brokers[(position.symbol, position.asset_type)].add(position.broker)
    for (symbol, asset_type), brokers in symbol_brokers.items():
        if len(brokers) > 1:
            issues.append(_issue(
                code="cross_broker_duplicate",
                severity="info",
                category="reconciliation",
                title=f"{symbol} appears across multiple brokers",
                description="This may be intentional, but review it when reconciling exposure and tax lots.",
                suggested_action="Confirm whether the duplicated exposure is expected.",
                evidence={"symbol": symbol, "asset_type": asset_type, "brokers": sorted(brokers)},
            ))

    cash_pct = cash_value / total_value if total_value else 0.0
    if total_value > 0 and cash_pct > HIGH_CASH_THRESHOLD:
        issues.append(_issue(
            code="high_cash_allocation",
            severity="info",
            category="allocation",
            title="Cash allocation is high",
            description=f"Cash is {cash_pct * 100:.1f}% of total portfolio value.",
            suggested_action="Confirm this is intentional or caused by unsettled/pending data.",
            evidence={"cash_value": cash_value, "total_value": total_value, "cash_pct": round(cash_pct, 4)},
        ))

    issues.sort(key=lambda item: (SEVERITY_RANK.get(item["severity"], 99), item["symbol"], item["code"]))
    counts = {severity: sum(1 for issue in issues if issue["severity"] == severity) for severity in SEVERITIES}
    if counts["critical"]:
        status = "high_risk"
    elif counts["warning"]:
        status = "needs_review"
    else:
        status = "clean"

    return {
        "generated_at": utcnow_iso(),
        "status": status,
        "issue_counts": counts,
        "total_issues": len(issues),
        "positions_checked": len(positions),
        "rules": [
            "missing_price",
            "missing_cost_basis",
            "missing_sector",
            "stale_price",
            "option_symbol_format",
            "tax_lot_quantity_mismatch",
            "cross_broker_duplicate",
            "high_cash_allocation",
        ],
        "issues": issues,
    }

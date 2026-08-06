from __future__ import annotations

import csv
import io
import math
import re

from backend.models import PositionIn

SYMBOL_COLS = {"symbol", "ticker", "instrument"}
NAME_COLS = {"name", "description", "security", "company"}
BROKER_COLS = {"broker", "account", "account name"}
QTY_COLS = {"quantity", "qty", "shares", "amount"}
AVG_COST_COLS = {
    "average cost",
    "avg cost",
    "average_cost",
    "cost basis per share",
    "avg_price",
    "average buy price",
    "average price",
}
PRICE_COLS = {
    "current price",
    "price",
    "last price",
    "market price",
    "current_price",
    "last_trade",
}
TYPE_COLS = {"type", "asset type", "asset_type", "security type"}
SECTOR_COLS = {"sector", "gics sector"}

SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,23}$")
ALLOWED_ASSET_TYPES = {"stock", "etf", "option", "crypto", "cash"}


def _match_col(headers: list[str], candidates: set[str]) -> str | None:
    normalized = {header.lower().strip(): header for header in headers}
    for candidate in candidates:
        if candidate in normalized:
            return normalized[candidate]
    return None


def parse_positions_csv(content: str, broker: str = "csv", max_rows: int = 2000) -> list[PositionIn]:
    reader = csv.DictReader(io.StringIO(content))
    headers = reader.fieldnames or []

    symbol_col = _match_col(headers, SYMBOL_COLS)
    name_col = _match_col(headers, NAME_COLS)
    broker_col = _match_col(headers, BROKER_COLS)
    qty_col = _match_col(headers, QTY_COLS)
    cost_col = _match_col(headers, AVG_COST_COLS)
    price_col = _match_col(headers, PRICE_COLS)
    type_col = _match_col(headers, TYPE_COLS)
    sector_col = _match_col(headers, SECTOR_COLS)

    if not symbol_col:
        raise ValueError(f"Could not find a symbol/ticker column. Headers found: {headers}")

    positions: list[PositionIn] = []
    for row_number, row in enumerate(reader, start=2):
        if row_number > max_rows + 1:
            raise ValueError(f"CSV import cannot exceed {max_rows} rows")

        symbol = (row.get(symbol_col) or "").strip().upper()
        if not symbol:
            continue
        if not SYMBOL_RE.fullmatch(symbol):
            raise ValueError(f"Invalid symbol at row {row_number}: {symbol}")

        asset_type = (row.get(type_col) or "stock").strip().lower() if type_col else "stock"
        if asset_type not in ALLOWED_ASSET_TYPES:
            raise ValueError(f"Invalid asset type at row {row_number}: {asset_type}")

        positions.append(
            PositionIn(
                symbol=symbol,
                name=(row.get(name_col) or symbol).strip() if name_col else symbol,
                broker=(row.get(broker_col) or broker).strip() if broker_col else broker,
                asset_type=asset_type,
                quantity=_safe_float(row.get(qty_col, "0")) if qty_col else 0.0,
                average_cost=_safe_float(row.get(cost_col, "0")) if cost_col else 0.0,
                current_price=_safe_float(row.get(price_col, "0")) if price_col else 0.0,
                sector=(row.get(sector_col) or "").strip() if sector_col else "",
            )
        )

    return positions


def _safe_float(value: str | None) -> float:
    if not value:
        return 0.0
    cleaned = str(value).replace("$", "").replace(",", "").replace("%", "").strip()
    try:
        parsed = float(cleaned)
    except ValueError:
        return 0.0
    return parsed if math.isfinite(parsed) else 0.0

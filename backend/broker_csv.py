"""Deterministic parsers for broker activity exports.

Smart Import's other doors — screenshots, PDFs, pasted text — go through a
vision model because their input has no schema. A broker's CSV export does
have one, and running it through a model instead is the wrong trade in three
directions at once: it costs tokens proportional to your entire trading
history, it is slow, and it is *non-deterministic*, so the same file imported
twice can yield different rows. For a ledger that returns are computed from,
that last one matters more than it sounds.

So: a real parser, chosen by matching the header row. The model is still there
for everything unrecognised, and for the formats nobody has written a parser
for yet.

Adding a broker means adding one `_Format` to `FORMATS`. The contract each
parser owes its caller:

* Never guess. A transaction code this module does not recognise is reported
  in ``unknown`` with the row it came from, not mapped to the nearest thing.
  A ledger with a plausible wrong row in it is worse than one with a hole,
  because the hole is visible.
* Emit the same shape the review table already renders, so a parsed file and
  an extracted screenshot land in the same UI.
* Give every row a stable ``external_id`` so re-importing an overlapping
  export is a no-op rather than a doubling.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

# Robinhood is commission-free, but regulatory pass-throughs (SEC fee, TAF)
# come out of a sale's proceeds, so quantity x price does not equal the amount
# banked. The residual is the fee — as long as it is small. Anything larger is
# a sign the row means something this parser has misread, so it is surfaced as
# a warning instead of being quietly booked as an enormous commission.
_FEE_ABSOLUTE_CEILING = 5.0
_FEE_RELATIVE_CEILING = 0.01

#: Contracts are priced per share; the cash moved is 100x that.
_OPTION_MULTIPLIER = 100


# --- small coercions -------------------------------------------------------

_PAREN = re.compile(r"^\((.*)\)$")
_NOT_NUMBER = re.compile(r"[^0-9.\-]")


def _money(raw: Any) -> float | None:
    """Parse a broker's idea of a dollar amount. None when there isn't one.

    Accounting parentheses mean negative, and every export uses them somewhere.
    Reading "($5.00)" as +5 would flip a fee into income.
    """
    text = str(raw or "").strip()
    if not text or text in {"-", "--", "N/A"}:
        return None
    negative = False
    match = _PAREN.match(text)
    if match:
        negative = True
        text = match.group(1)
    text = _NOT_NUMBER.sub("", text)
    if text.count("-") > 1 or not text.strip("-."):
        return None
    if text.startswith("-"):
        negative = True
        text = text[1:]
    try:
        value = float(text)
    except ValueError:
        return None
    return -value if negative else value


def _quantity(raw: Any) -> float:
    value = _money(raw)
    return abs(value) if value is not None else 0.0


def _iso_date(raw: Any) -> str | None:
    """Normalise a broker date to ISO. None when it cannot be read.

    Deliberately strict about the year: a two-digit year is ambiguous enough
    that placing the row in the wrong decade is a real outcome, and a
    transaction on the wrong date silently relocates a return.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    match = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if match:
        month, day, year = (int(part) for part in match.groups())
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None
    return None


def _clean_symbol(raw: Any) -> str:
    return re.sub(r"[^A-Z0-9.\-]", "", str(raw or "").strip().upper())[:24]


# --- the row a parser produces --------------------------------------------


@dataclass
class ParsedRow:
    """One ledger row, in the shape the review table and bulk import expect."""

    occurred_at: str
    action: str
    symbol: str = ""
    quantity: float = 0.0
    price: float = 0.0
    fee: float = 0.0
    asset_type: str = "stock"
    broker: str = "manual"
    notes: str = ""
    external_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "occurred_at": self.occurred_at,
            "action": self.action,
            "symbol": self.symbol,
            "quantity": round(self.quantity, 8),
            "price": round(self.price, 6),
            "fee": round(self.fee, 4),
            "asset_type": self.asset_type,
            "broker": self.broker,
            "notes": self.notes,
            "external_id": self.external_id,
        }


@dataclass
class ParseResult:
    broker: str
    label: str
    transactions: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: Rows whose transaction code this parser does not know. Reported rather
    #: than guessed — see the module docstring.
    unknown: list[dict[str, str]] = field(default_factory=list)
    ignored: int = 0
    total_rows: int = 0


# --- Robinhood -------------------------------------------------------------

# Robinhood's account activity report. Codes are the "Trans Code" column.
#
# Mapped to Serin's closed action set. Where a code's direction depends on the
# money (a transfer can go either way, interest can be earned or charged), the
# mapping is a callable that reads the amount.
_RH_SIMPLE: dict[str, str] = {
    "BUY": "buy",
    "SELL": "sell",
    # Options. Open/close is a position concept; the ledger only needs the
    # direction of the trade.
    "BTO": "buy",
    "BTC": "buy",
    "STO": "sell",
    "STC": "sell",
    # Income.
    "CDIV": "dividend",
    "DIV": "dividend",
    "MDIV": "dividend",
    # Withholding on a dividend, and the reversal of one.
    "DTAX": "tax",
    "DWT": "tax",
    # Costs that are unambiguously costs.
    "GOLD": "fee",
    "DFEE": "fee",
    "AFEE": "fee",
    "FEE": "fee",
    "TAF": "fee",
    "SEC": "fee",
    # Corporate actions that change share count without moving cash.
    "SPL": "split",
    "SPR": "split",
    # Shares moving in or out without a sale (ACATS, gifts, DRIP receipts).
    "REC": "transfer",
    "SPP": "transfer",
    # An expired option: the position ends, no cash changes hands.
    "OEXP": "adjustment",
    "CONV": "adjustment",
    "MISC": "adjustment",
}

_RH_OPTION_CODES = frozenset({"BTO", "BTC", "STO", "STC", "OEXP"})

#: Codes whose meaning is the sign of the amount, not the code itself.
_RH_SIGNED: dict[str, tuple[str, str]] = {
    # code: (action when money came in, action when money went out)
    "ACH": ("deposit", "withdrawal"),
    "RTP": ("deposit", "withdrawal"),
    "IRA": ("deposit", "withdrawal"),
    "WIRE": ("deposit", "withdrawal"),
    "CSD": ("deposit", "withdrawal"),
    "CSW": ("deposit", "withdrawal"),
    # Interest earned on cash, or interest charged on margin.
    "INT": ("interest", "fee"),
    "MINT": ("interest", "fee"),
}

_RH_HEADERS = (
    "activity date",
    "process date",
    "settle date",
    "instrument",
    "description",
    "trans code",
    "quantity",
    "price",
    "amount",
)


def _parse_robinhood(rows: list[dict[str, str]]) -> ParseResult:
    result = ParseResult(broker="robinhood", label="Robinhood")
    seen: Counter[str] = Counter()
    undated = 0

    for row in rows:
        code = str(row.get("trans code") or "").strip().upper()
        description = str(row.get("description") or "").strip()
        occurred_at = _iso_date(row.get("activity date")) or _iso_date(row.get("process date"))

        if not code and not description:
            result.ignored += 1  # blank spacer row
            continue

        action = _RH_SIMPLE.get(code)
        amount = _money(row.get("amount"))
        if action is None and code in _RH_SIGNED:
            inbound, outbound = _RH_SIGNED[code]
            if amount is None:
                result.unknown.append({"code": code, "description": description,
                                       "reason": "no amount, so its direction is unreadable"})
                continue
            action = inbound if amount >= 0 else outbound

        if action is None:
            result.unknown.append({"code": code, "description": description,
                                   "reason": "unrecognised transaction code"})
            continue

        if not occurred_at:
            # An undated row cannot be placed in a return series, and inventing
            # a date moves somebody's performance to a day that never happened.
            undated += 1
            continue

        symbol = _clean_symbol(row.get("instrument"))
        quantity = _quantity(row.get("quantity"))
        price = abs(_money(row.get("price")) or 0.0)
        is_option = code in _RH_OPTION_CODES
        fee = 0.0
        notes = description[:200]

        if action in ("buy", "sell"):
            if not symbol:
                result.unknown.append({"code": code, "description": description,
                                       "reason": "a trade with no instrument cannot be replayed"})
                continue
            if quantity <= 0:
                result.unknown.append({"code": code, "description": description,
                                       "reason": "a trade of zero shares is not a trade"})
                continue
            if not price and amount:
                divisor = quantity * (_OPTION_MULTIPLIER if is_option else 1)
                price = abs(amount) / divisor if divisor else 0.0
            if not is_option and price and amount is not None:
                fee, note = _infer_trade_fee(action, quantity, price, amount)
                if note:
                    result.warnings.append(f"{occurred_at} {symbol}: {note}")
        elif action in ("split", "adjustment", "transfer"):
            price = 0.0
        else:
            # Cash rows: dividend, interest, fee, tax, deposit, withdrawal. The
            # ledger carries the money in `price`, which is what the amount
            # column holds. Quantity is meaningless here and the review table
            # shows the column as "price / amount" for exactly this reason.
            quantity = 0.0
            price = abs(amount or 0.0)
            if price == 0:
                result.ignored += 1
                continue

        parsed = ParsedRow(
            occurred_at=occurred_at,
            action=action,
            symbol=symbol,
            quantity=quantity,
            price=price,
            fee=fee,
            asset_type="option" if is_option else "stock",
            broker="robinhood",
            notes=notes,
            external_id="",
        )
        parsed.external_id = _stable_id(parsed, seen)
        result.transactions.append(parsed.as_dict())

    if undated:
        result.warnings.append(
            f"{undated} row{'s' if undated != 1 else ''} had no readable date and "
            "were left out — a transaction without a date cannot be placed in a "
            "return series."
        )
    return result


def _infer_trade_fee(
    action: str, quantity: float, price: float, amount: float
) -> tuple[float, str]:
    """Recover the regulatory fee baked into a trade's net amount.

    Robinhood charges no commission, but a sale's proceeds arrive net of the
    SEC fee and TAF. The gap between quantity x price and the amount banked is
    therefore the fee — for a plausibly small gap. A large one means this row
    is not what the parser thinks it is, so it is reported rather than booked:
    a $4,000 "commission" would corrupt the cost basis it was meant to refine.
    """
    gross = quantity * price
    if gross <= 0:
        return 0.0, ""
    residual = (gross - amount) if action == "sell" else (abs(amount) - gross)
    if residual <= 0:
        return 0.0, ""
    ceiling = max(_FEE_ABSOLUTE_CEILING, gross * _FEE_RELATIVE_CEILING)
    if residual > ceiling:
        return 0.0, (
            f"quantity x price is {gross:,.2f} but the amount is {abs(amount):,.2f} — "
            "imported without a fee, worth checking against your statement"
        )
    return round(residual, 4), ""


def _stable_id(row: ParsedRow, seen: Counter[str]) -> str:
    """A re-import-safe id for a row the broker gave no reference for.

    Content-hashed, so the same row in the same export always lands on the same
    id. The occurrence counter is what makes genuinely repeated rows work: two
    identical $50 recurring buys on one day are two rows, and get #1 and #2. A
    later, longer export containing both produces the same two ids, so the
    overlap dedupes and only what is new is written.
    """
    seed = "|".join(
        str(part) for part in (
            row.occurred_at, row.action, row.symbol,
            f"{row.quantity:.8f}", f"{row.price:.6f}", f"{row.fee:.4f}",
        )
    )
    digest = hashlib.sha256(seed.encode()).hexdigest()[:20]
    seen[digest] += 1
    return f"{digest}#{seen[digest]}"


# --- format registry -------------------------------------------------------


@dataclass(frozen=True)
class _Format:
    broker: str
    label: str
    #: Header cells that must all be present for this format to claim a file.
    required: tuple[str, ...]
    parse: Callable[[list[dict[str, str]]], ParseResult]


FORMATS: tuple[_Format, ...] = (
    _Format(
        broker="robinhood",
        label="Robinhood",
        # "Instrument" and "Trans Code" together are the signature: plenty of
        # exports have a date and an amount, almost none use those two names.
        required=("activity date", "instrument", "trans code", "amount"),
        parse=_parse_robinhood,
    ),
)


def _read_rows(text: str) -> list[dict[str, str]]:
    """CSV or TSV to lowercase-keyed dicts. Empty when it does not parse."""
    sample = text[:4096]
    delimiter = "\t" if sample.count("\t") > sample.count(",") else ","
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")), delimiter=delimiter)
    rows: list[dict[str, str]] = []
    for raw in reader:
        rows.append({
            str(key or "").strip().lower(): ("" if value is None else str(value))
            for key, value in raw.items()
        })
    return rows


def detect(text: str) -> _Format | None:
    """Which known broker export this is, or None to fall through to the model."""
    if not text or "," not in text and "\t" not in text:
        return None
    try:
        reader = csv.reader(io.StringIO(text.lstrip("﻿")[:8192]))
        header = next(reader, [])
    except (csv.Error, StopIteration):
        return None
    cells = {str(cell or "").strip().lower() for cell in header}
    for fmt in FORMATS:
        if all(name in cells for name in fmt.required):
            return fmt
    return None


def parse(text: str) -> ParseResult | None:
    """Parse a broker export, or None when the format is not recognised."""
    fmt = detect(text)
    if fmt is None:
        return None
    rows = _read_rows(text)
    if not rows:
        return None
    result = fmt.parse(rows)
    result.total_rows = len(rows)
    return result


def summarise(result: ParseResult) -> str:
    """The one-line note the import screen shows above the review table."""
    count = len(result.transactions)
    parts = [
        f"Read {count} transaction{'' if count == 1 else 's'} from your "
        f"{result.label} activity export."
    ]
    if result.unknown:
        codes = sorted({row["code"] for row in result.unknown if row.get("code")})
        shown = ", ".join(codes[:6]) + ("…" if len(codes) > 6 else "")
        one_row, one_code = len(result.unknown) == 1, len(codes) == 1
        parts.append(
            f"{len(result.unknown)} row{'' if one_row else 's'} used a transaction code"
            if one_code else
            f"{len(result.unknown)} row{'' if one_row else 's'} used transaction codes"
        )
        parts[-1] += (
            f" Serin does not know yet ({shown}) and "
            f"{'was' if one_row else 'were'} left out rather than guessed at."
        )
    parts.append("No AI was used — this format is parsed exactly.")
    return " ".join(parts)

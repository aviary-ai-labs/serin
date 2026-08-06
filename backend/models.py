from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

AssetType = Literal["stock", "etf", "option", "crypto", "cash"]
BriefingStyle = Literal["operator", "analyst", "executive"]


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


class PositionIn(BaseModel):
    symbol: str
    name: str = ""
    broker: str = "manual"
    asset_type: AssetType = "stock"
    quantity: float = 0.0
    average_cost: float = 0.0
    current_price: float = 0.0
    sector: str = ""
    # ISO 4217 code of the position's native prices (average_cost /
    # current_price). Aggregates are converted to the display currency.
    currency: str = "USD"

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        symbol = value.strip().upper()
        if not symbol:
            raise ValueError("symbol is required")
        if len(symbol) > 24:
            raise ValueError("symbol is too long")
        return symbol

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        code = (value or "USD").strip().upper()
        if not code:
            return "USD"
        if len(code) != 3 or not code.isalpha():
            raise ValueError("currency must be a 3-letter ISO code")
        return code

    @field_validator("broker")
    @classmethod
    def normalize_broker(cls, value: str) -> str:
        broker = value.strip().lower().replace(" ", "_")
        return broker or "manual"

    @field_validator("quantity", "average_cost", "current_price")
    @classmethod
    def non_negative(cls, value: float) -> float:
        if value < 0:
            raise ValueError("value must be >= 0")
        return value


class Position(PositionIn):
    id: int
    market_value: float = 0.0
    total_cost: float = 0.0
    unrealized_gain: float = 0.0
    unrealized_gain_pct: float = 0.0
    updated_at: str = Field(default_factory=utcnow_iso)
    source: Literal["manual", "csv", "snaptrade"] = "manual"


class TaxLotIn(BaseModel):
    symbol: str
    broker: str = "manual"
    quantity: float = 0.0
    cost_basis: float = 0.0
    acquired_at: str

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        symbol = value.strip().upper()
        if not symbol:
            raise ValueError("symbol is required")
        if len(symbol) > 24:
            raise ValueError("symbol is too long")
        return symbol

    @field_validator("broker")
    @classmethod
    def normalize_broker(cls, value: str) -> str:
        broker = value.strip().lower().replace(" ", "_")
        return broker or "manual"

    @field_validator("quantity", "cost_basis")
    @classmethod
    def positive_value(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("value must be > 0")
        return value

    @field_validator("acquired_at")
    @classmethod
    def normalize_acquired_at(cls, value: str) -> str:
        date = value.strip()
        if not date:
            raise ValueError("purchase date is required")
        return date


class TaxLot(TaxLotIn):
    id: int
    current_price: float = 0.0
    market_value: float = 0.0
    unrealized_gain: float = 0.0
    unrealized_gain_pct: float = 0.0
    holding_period: Literal["short-term", "long-term"] = "short-term"
    days_to_long_term: int = 0
    created_at: str = Field(default_factory=utcnow_iso)


class PortfolioSummary(BaseModel):
    total_value: float = 0.0
    total_cost: float = 0.0
    total_gain: float = 0.0
    total_gain_pct: float = 0.0
    cash_value: float = 0.0
    positions: list[Position] = Field(default_factory=list)
    broker_breakdown: dict[str, float] = Field(default_factory=dict)
    sector_breakdown: dict[str, float] = Field(default_factory=dict)
    top_positions: list[Position] = Field(default_factory=list)
    last_refresh: str | None = None


class Briefing(BaseModel):
    id: int
    status: Literal["running", "done", "error"]
    summary: str = ""
    output_markdown: str = ""
    model: str = ""
    model_cost_usd: float = 0.0
    trigger: Literal["manual", "scheduled"] = "manual"
    emailed_at: str | None = None
    created_at: str
    completed_at: str | None = None
    error: str = ""


class ScheduleIn(BaseModel):
    enabled: bool = False
    time: str = "07:30"
    timezone: str = "local"
    email_enabled: bool = False

    @field_validator("time")
    @classmethod
    def validate_time(cls, value: str) -> str:
        text = value.strip()
        parts = text.split(":")
        if len(parts) != 2:
            raise ValueError("time must be HH:MM")
        hour, minute = parts
        if not (hour.isdigit() and minute.isdigit() and 0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
            raise ValueError("time must be HH:MM (24-hour)")
        return f"{int(hour):02d}:{int(minute):02d}"

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        name = value.strip() or "local"
        if name == "local":
            return name
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(name)
        except Exception as exc:
            raise ValueError(f"unknown timezone: {name}") from exc
        return name


class BriefingPreferencesIn(BaseModel):
    style: BriefingStyle = "operator"


# --- transactions ----------------------------------------------------------

TransactionAction = Literal[
    "buy", "sell", "dividend", "interest", "fee", "cash_in", "cash_out", "split", "transfer"
]


class TransactionIn(BaseModel):
    symbol: str = ""
    broker: str = "manual"
    asset_type: AssetType = "stock"
    action: TransactionAction = "buy"
    quantity: float = 0.0
    price: float = 0.0
    fee: float = 0.0
    currency: str = "USD"
    occurred_at: str
    notes: str = ""

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()[:24]

    @field_validator("broker")
    @classmethod
    def normalize_broker(cls, value: str) -> str:
        broker = value.strip().lower().replace(" ", "_")
        return broker or "manual"

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return (value or "USD").strip().upper()[:8] or "USD"

    @field_validator("occurred_at")
    @classmethod
    def normalize_occurred(cls, value: str) -> str:
        date = (value or "").strip()
        if not date:
            raise ValueError("occurred_at is required (YYYY-MM-DD or ISO timestamp)")
        return date

    @field_validator("quantity", "price", "fee")
    @classmethod
    def non_negative(cls, value: float) -> float:
        if value < 0:
            raise ValueError("must be >= 0")
        return value


class Transaction(TransactionIn):
    id: int
    amount: float = 0.0  # signed dollar impact (negative = cash out, positive = cash in)
    source: Literal["manual", "csv", "snaptrade", "import"] = "manual"
    created_at: str = Field(default_factory=utcnow_iso)


# --- accounts --------------------------------------------------------------

AccountKind = Literal["taxable", "ira", "roth_ira", "401k", "hsa", "crypto", "savings", "other"]


class AccountIn(BaseModel):
    name: str
    kind: AccountKind = "taxable"
    broker: str = "manual"
    currency: str = "USD"

    @field_validator("name")
    @classmethod
    def name_required(cls, value: str) -> str:
        name = (value or "").strip()
        if not name:
            raise ValueError("account name is required")
        return name[:80]

    @field_validator("broker")
    @classmethod
    def normalize_broker(cls, value: str) -> str:
        broker = value.strip().lower().replace(" ", "_")
        return broker or "manual"

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return (value or "USD").strip().upper()[:8] or "USD"


class Account(AccountIn):
    id: int
    market_value: float = 0.0
    total_cost: float = 0.0
    cash_value: float = 0.0
    position_count: int = 0
    created_at: str = Field(default_factory=utcnow_iso)

"""Connector SDK — the core extensibility primitive for Serin.

A *connector* is a self-contained, in-tree plugin that brings data into Serin
(holdings, market data) or acts on it (insights). Each connector ships:

  1. a ``ConnectorManifest`` (id, kind, human metadata, and a config schema
     that the portal renders into a form), and
  2. an implementation of its kind's interface.

The portal never hardcodes connector-specific UI: it reads the manifest's
``config_schema`` and generates the form. Adding a connector = dropping a module
under ``backend/connectors/<kind>/`` and registering it. No UI changes needed.

Kinds:
  - ``market_data`` — quotes, history, fundamentals (Yahoo, FMP, …)
  - ``holdings``    — where positions come from (SnapTrade, CSV, …)
  - ``insight``     — consumes the portfolio, produces output (AI briefing, …)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

ConnectorKind = Literal["market_data", "holdings", "insight"]
FieldType = Literal[
    "text", "password", "url", "number", "select", "textarea", "boolean", "mapping"
]


@dataclass
class ConfigField:
    """One configurable input for a connector, rendered by the portal."""

    key: str
    label: str
    type: FieldType = "text"
    required: bool = False
    default: Any = ""
    help: str = ""
    placeholder: str = ""
    # For type="select": [{"value": "...", "label": "..."}]
    options: list[dict[str, str]] = field(default_factory=list)
    # Secret fields are never returned in plaintext by the API.
    secret: bool = False
    # Who this value belongs to, which decides where it is stored:
    #
    #   "instance" — the deployment's. One market-data key serves everyone, and
    #                background work that touches it has no user at all.
    #   "user"     — the person's. Their own exchange key, their own
    #                preferences. On a shared deployment these must not be one
    #                blob, or one customer's crypto keys are readable and
    #                overwritable by the next.
    #
    # Defaults to "instance" because that is how every field behaved before
    # this existed, and because it is the safe direction to be wrong in: a
    # user's value wrongly shared is a breach, an operator's value wrongly
    # split is a re-entered API key. Single-user installs are unaffected either
    # way — there is only one scope to be in.
    owner: Literal["instance", "user"] = "instance"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ConnectorManifest:
    id: str
    name: str
    kind: ConnectorKind
    description: str
    icon: str = "ti-plug"  # tabler icon name, rendered by the portal
    version: str = "1.0.0"
    author: str = "serin"
    docs_url: str = ""
    config_schema: list[ConfigField] = field(default_factory=list)
    # Insight/holdings connectors are off until the user opts in; market data
    # is on by default so a fresh install can fetch prices immediately.
    default_enabled: bool = True
    # Market-data scope: "all" competes to be the main price source; a
    # specialized scope (e.g. "crypto") only serves that asset type and is
    # layered on top of the main provider instead of replacing it.
    asset_scope: str = "all"
    # How the user connects — drives the portal's trust-first grouping and the
    # hosted "no raw secrets" rule (see docs/CONNECTOR-TRUST.md):
    #   "oauth"   → Connect: authorize on the provider's own site (token, no secret)
    #   "file"    → Import: hand over a statement, no standing access
    #   "api_key" → Advanced: paste a raw read-only key (self-host / power users)
    #   "none"    → no credentials needed (public/free data)
    connect_method: str = "api_key"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "description": self.description,
            "icon": self.icon,
            "version": self.version,
            "author": self.author,
            "docs_url": self.docs_url,
            "default_enabled": self.default_enabled,
            "asset_scope": self.asset_scope,
            "connect_method": self.connect_method,
            "config_schema": [f.to_dict() for f in self.config_schema],
        }


@dataclass
class TestResult:
    ok: bool
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "message": self.message}


class Connector:
    """Base class. Subclass per kind; set ``manifest`` on the subclass."""

    manifest: ConnectorManifest

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}

    # Config helpers ---------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        value = self.config.get(key, None)
        if value in (None, ""):
            # Fall back to the field default declared in the manifest.
            for field_def in self.manifest.config_schema:
                if field_def.key == key and field_def.default not in (None, ""):
                    return field_def.default
            return default
        return value

    # Every connector can be health-checked. Default: trivially OK.
    def test(self) -> TestResult:
        return TestResult(ok=True, message="No connection test for this connector.")


class MarketDataConnector(Connector):
    """Provides quotes, history and single-symbol detail."""

    def refresh_prices(self, positions) -> dict:
        raise NotImplementedError

    def fetch_history(self, period: str, symbols, positions_by_symbol) -> dict:
        raise NotImplementedError

    def quote(self, symbol: str, asset_type: str) -> dict | None:
        raise NotImplementedError

    def fetch_fundamentals(self, symbol: str, asset_type: str = "stock") -> dict | None:
        """Optional capability: per-symbol fundamentals, or None if unsupported.

        Normalized keys (any may be None): ``name``, ``kind`` ("stock" | "etf" |
        "fund"), ``market_cap``, ``pe_ratio``, ``pb_ratio``, ``beta``,
        ``dividend_yield`` (decimal, 0.013 = 1.3%), ``expense_ratio`` (decimal,
        0.0003 = 3 bps). ``backend.fundamentals`` walks the market-data chain
        and caches results; connectors without the capability simply fall
        through via this default.
        """
        return None


class HoldingsConnector(Connector):
    """Brings positions into Serin (broker sync, file import, manual, …)."""

    # Whether this connector supports an on-demand pull (vs. one-shot upload).
    supports_sync: bool = False

    def sync(self) -> dict:
        """Pull current holdings. Returns a summary dict."""
        raise NotImplementedError

    def status(self) -> dict:
        """Connection status detail for the portal (accounts, last sync, …)."""
        return {}


class InsightConnector(Connector):
    """Consumes the portfolio snapshot, produces an output (briefing, etc.)."""

    def run(self, context: dict | None = None) -> dict:
        raise NotImplementedError

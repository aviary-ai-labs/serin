"""SnapTrade holdings connector — read-only multi-broker sync.

Credentials resolve portal-first (this connector's config form), falling back
to ``SNAPTRADE_CLIENT_ID`` / ``SNAPTRADE_CONSUMER_KEY`` env vars — the same
pattern as the AI-briefing keys (backend.ai_provider).
"""

from __future__ import annotations

from backend import snaptrade
from backend.connectors.base import (
    ConfigField,
    ConnectorManifest,
    HoldingsConnector,
    TestResult,
)
from backend.connectors.registry import register


@register
class SnapTradeConnector(HoldingsConnector):
    supports_sync = True
    manifest = ConnectorManifest(
        id="snaptrade",
        name="SnapTrade",
        kind="holdings",
        description="Read-only sync of holdings and cash from 20+ brokerages (Robinhood, Schwab, Fidelity, E*Trade, Coinbase, …). Serin can never place trades.",
        icon="ti-building-bank",
        docs_url="https://snaptrade.com",
        config_schema=[
            ConfigField(
                key="client_id",
                label="Client ID",
                type="text",
                help="From your SnapTrade dashboard (free tier available).",
            ),
            ConfigField(
                key="consumer_key",
                label="Consumer Key",
                type="password",
                secret=True,
                help="Kept locally; used only to talk to SnapTrade.",
            ),
            ConfigField(
                key="auto_sync_daily",
                owner="user",
                label="Auto-sync holdings daily",
                type="boolean",
                default=False,
                help="Pull fresh holdings once a day on the scheduler (also catches up on launch).",
            ),
        ],
        default_enabled=True,
        connect_method="oauth",  # authorize at the broker via SnapTrade's portal
    )

    def status(self) -> dict:
        try:
            return snaptrade.status()
        except Exception as exc:
            return {"configured": snaptrade.snaptrade_available(), "error": snaptrade.error_message(exc)}

    def sync(self) -> dict:
        return snaptrade.sync()

    def test(self) -> TestResult:
        if not snaptrade.snaptrade_available():
            return TestResult(
                ok=False,
                message="Add your SnapTrade Client ID and Consumer Key above (or set SNAPTRADE_CLIENT_ID / SNAPTRADE_CONSUMER_KEY in .env).",
            )
        try:
            status = snaptrade.status()
        except Exception as exc:
            return TestResult(ok=False, message=snaptrade.error_message(exc))
        connections = status.get("connections") or []
        if connections:
            return TestResult(ok=True, message=f"{len(connections)} brokerage connection(s) linked.")
        return TestResult(ok=True, message="SnapTrade configured — no brokerages linked yet.")

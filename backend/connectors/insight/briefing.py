"""AI daily briefing — an optional insight connector (default OFF).

The briefing is no longer core: it's a connector you toggle on. When enabled,
the Briefings tab and scheduler are active; when off, Serin is a pure tracker.
Provider/model/style chosen here drive the briefing run.
"""

from __future__ import annotations

from backend.config import get_ai_status
from backend.connectors.base import (
    ConfigField,
    ConnectorManifest,
    InsightConnector,
    TestResult,
)
from backend.connectors.registry import register


@register
class BriefingConnector(InsightConnector):
    manifest = ConnectorManifest(
        id="ai_briefing",
        name="AI daily briefing",
        kind="insight",
        description="A daily plain-language briefing on what changed in your portfolio, market context, and risk flags. Context, not investment advice.",
        icon="ti-sparkles",
        default_enabled=False,  # opt-in
        config_schema=[
            ConfigField(
                key="provider",
                label="AI provider",
                type="select",
                default="auto",
                options=[
                    {"value": "auto", "label": "Auto (prefer Anthropic, then DeepSeek)"},
                    {"value": "anthropic_api", "label": "Anthropic API"},
                    {"value": "deepseek", "label": "DeepSeek API"},
                    {"value": "claude_cli", "label": "Claude CLI (local dev)"},
                ],
                help="Which model backend generates the briefing.",
            ),
            ConfigField(
                key="anthropic_api_key",
                label="Anthropic API key",
                type="password",
                secret=True,
                help="Used when provider is Anthropic or Auto.",
            ),
            ConfigField(
                key="deepseek_api_key",
                label="DeepSeek API key",
                type="password",
                secret=True,
                help="Used when provider is DeepSeek. ~1/20th the cost of Claude Sonnet.",
            ),
            ConfigField(
                key="style",
                owner="user",
                label="Default briefing style",
                type="select",
                default="operator",
                options=[
                    {"value": "operator", "label": "Operator — structured daily review"},
                    {"value": "analyst", "label": "Analyst — deeper themes & interpretation"},
                    {"value": "executive", "label": "Executive — 60-second summary"},
                ],
            ),
        ],
    )

    def test(self) -> TestResult:
        status = get_ai_status(force=True)
        if status.get("ready"):
            return TestResult(ok=True, message=f"Ready via {status.get('provider')} ({status.get('model')}).")
        return TestResult(ok=False, message=status.get("error") or "No AI provider configured.")

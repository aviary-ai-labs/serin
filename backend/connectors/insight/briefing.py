"""AI daily briefing — an optional insight connector (default OFF).

The briefing is no longer core: it's a connector you toggle on. When enabled,
the Briefings tab and scheduler are active; when off, Serin is a pure tracker.
Providers/models/style chosen here drive the briefing run (and Smart Import).
"""

from __future__ import annotations

from backend import ai_catalog
from backend.config import get_ai_status
from backend.connectors.base import (
    ConfigField,
    ConnectorManifest,
    InsightConnector,
    TestResult,
)
from backend.connectors.registry import register


def _provider_key_fields() -> list[ConfigField]:
    """One secret field per catalog provider that authenticates with a key.

    Flat fields rather than keys inside the providers list, deliberately: the
    registry's secret machinery (encrypt at rest, mask on read, blank-means-
    keep on save) works per field, and a JSON blob would opt out of all of it.
    The portal renders each inside its provider row.
    """
    return [
        ConfigField(
            key=spec.key_field,
            label=f"{spec.label} API key",
            type="password",
            secret=True,
            help=spec.help,
        )
        for spec in ai_catalog.CATALOG
        if spec.needs_key
    ]


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
                key="providers",
                label="AI providers",
                type="provider_list",
                options=ai_catalog.as_options(),
                help="Tried top to bottom — drag to reorder. Each provider keeps its own key and model.",
            ),
            *_provider_key_fields(),
            # No "default briefing style" here, deliberately. The style that
            # actually runs — manual and scheduled both — is the Briefings
            # tab's preference (db.get_briefing_preferences); a second control
            # here was read by nothing and could only teach people the wrong
            # place to change it.
        ],
    )

    @classmethod
    def ready(cls) -> bool:
        """Ready means the waterfall has at least one usable provider —
        portal keys and deployment env vars both count."""
        from backend import ai_provider

        try:
            return bool(ai_provider.provider_chain())
        except Exception:
            return True

    def test(self) -> TestResult:
        status = get_ai_status(force=True)
        if status.get("ready"):
            if status.get("managed"):
                # Whose model serves managed AI is Serin's implementation
                # detail, not the customer's configuration.
                return TestResult(ok=True, message="Ready via Serin managed AI — included with your plan.")
            return TestResult(ok=True, message=f"Ready via {status.get('provider')} ({status.get('model')}).")
        return TestResult(ok=False, message=status.get("error") or "No AI provider configured.")

"""Portal-aware AI provider/key resolution.

The AI-briefing connector stores the provider choice and API keys in the
connector portal (``app_settings`` key ``connector:ai_briefing:config``);
environment variables are the deployment-level fallback. This module is the
single source of truth for "which provider/key does Serin actually use" —
briefings, smart import, and the status endpoint must all resolve through
here so a key saved in the UI works everywhere, not just in some code paths.
"""

from __future__ import annotations

from backend.config import settings
from backend.connectors import registry as connector_registry


def _portal_config() -> dict:
    try:
        return connector_registry.get_config("ai_briefing") or {}
    except Exception:  # registry unavailable (partial startup) — env only
        return {}


def resolved_anthropic_key() -> str:
    cfg = _portal_config()
    return (cfg.get("anthropic_api_key") or "").strip() or settings.anthropic_api_key.strip()


def resolved_deepseek_key() -> str:
    cfg = _portal_config()
    return (cfg.get("deepseek_api_key") or "").strip() or settings.deepseek_api_key.strip()


def billing_subject() -> str:
    """Who an AI request is *for*, when that differs from who holds the key.

    Empty on a single-user install: the licence holder and the person are the
    same, and there is nothing to distinguish.

    On a shared deployment they are not the same. One managed-AI licence
    covers the whole fleet, so a metering key derived from the licence counts
    every customer against one budget — the first heavy user exhausts it and
    everyone else gets refused for the rest of the month. This names the
    account so the meter can be per person.

    Only ever a hint to our own proxy, which enforces a ceiling per licence as
    well as per subject; a forged value cannot buy more than the deployment
    already paid for.
    """
    from backend import scope

    if not scope.provider_installed():
        return ""
    try:
        return scope.current()
    except Exception:
        # Background work with nobody in context. Better unattributed than
        # failing the request over a metering label.
        return ""


def anthropic_headers() -> dict[str, str]:
    """Headers for a Messages API call, whether it lands on Anthropic or on
    Serin's managed proxy. Anthropic ignores the extra header; the proxy meters
    on it."""
    headers = {
        "x-api-key": resolved_anthropic_key(),
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    subject = billing_subject()
    if subject:
        headers["x-serin-subject"] = subject
    return headers


def anthropic_available() -> bool:
    return bool(resolved_anthropic_key())


def deepseek_available() -> bool:
    return bool(resolved_deepseek_key())


def resolved_provider() -> str:
    """Which provider an AI call should use.

    Precedence: explicit portal choice → explicit ``SERIN_AI_PROVIDER`` →
    auto (DeepSeek, then Anthropic, then Claude CLI). Availability checks are
    portal-aware, unlike ``settings.resolved_ai_provider`` which only sees
    environment variables.

    Auto prefers DeepSeek on measured cost: benchmarked against the real
    briefing prompt, deepseek-v4-flash produced a briefing for roughly a
    fortieth of what claude-sonnet-4-6 charged, in a third of the time, and was
    the only model in the field to do the arithmetic on a planted
    sector-total inconsistency. Anyone who prefers otherwise still wins the
    tie — a portal choice and SERIN_AI_PROVIDER both outrank this, and auto
    only decides when someone has configured both and expressed no preference.
    """
    cfg = _portal_config()
    requested = str(cfg.get("provider") or "").strip().lower()
    if requested in ("", "auto"):
        requested = settings.ai_provider.strip().lower()
    if requested == "claude_cli":
        return "claude_cli" if settings.claude_cli_configured else "none"
    if requested == "anthropic_api":
        return "anthropic_api" if anthropic_available() else "none"
    if requested == "deepseek":
        return "deepseek" if deepseek_available() else "none"
    if deepseek_available():
        return "deepseek"
    if anthropic_available():
        return "anthropic_api"
    if settings.claude_cli_configured:
        return "claude_cli"
    return "none"

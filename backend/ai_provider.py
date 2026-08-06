"""Portal-aware AI provider/key resolution.

The AI-briefing connector stores the provider choice and API keys in the
connector portal (``app_settings`` key ``connector:ai_briefing:config``);
environment variables are the deployment-level fallback. This module is the
single source of truth for "which provider/key does Serin actually use" —
briefings, smart import, and the status endpoint must all resolve through
here so a key saved in the UI works everywhere, not just in some code paths.
"""

from __future__ import annotations

import json
import os

from backend import ai_catalog
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


def _entry_key(cfg: dict, spec: ai_catalog.ProviderSpec) -> str:
    """The API key for one provider: portal value first, then deployment env.

    Anthropic and DeepSeek go through settings rather than os.environ so the
    existing configuration surface (and every test that monkeypatches it)
    keeps working.
    """
    if not spec.needs_key:
        return ""
    val = (cfg.get(spec.key_field) or "").strip()
    if val:
        return val
    if spec.id == "anthropic":
        return settings.anthropic_api_key.strip()
    if spec.id == "deepseek":
        return settings.deepseek_api_key.strip()
    return os.environ.get(spec.env_var, "").strip() if spec.env_var else ""


def _default_model(spec: ai_catalog.ProviderSpec) -> str:
    if spec.id == "anthropic":
        return settings.anthropic_model.strip() or spec.default_model
    if spec.id == "deepseek":
        return settings.deepseek_model.strip() or spec.default_model
    return spec.default_model


def _resolve_entry(cfg: dict, raw: dict) -> dict | None:
    """One chain row → a callable provider, or None if it can't work."""
    spec = ai_catalog.get(str(raw.get("id") or "").strip().lower())
    if spec is None:
        return None
    if spec.kind == "claude_cli":
        if not settings.claude_cli_configured:
            return None
        key = ""
    else:
        key = _entry_key(cfg, spec)
        if spec.needs_key and not key:
            return None
    base_url = (raw.get("base_url") or "").strip() or spec.base_url
    if spec.id == "deepseek" and not (raw.get("base_url") or "").strip():
        base_url = settings.deepseek_base_url.rstrip("/") or spec.base_url
    explicit_model = (raw.get("model") or "").strip()
    return {
        "id": spec.id,
        "kind": spec.kind,
        "label": spec.label,
        "key": key,
        "model": explicit_model or _default_model(spec),
        # Whether the user chose the model or inherited the default — Smart
        # Import downgrades defaulted models to a cheap one, but never
        # overrides an explicit choice.
        "model_explicit": bool(explicit_model),
        "base_url": base_url,
        "vision": spec.vision,
    }


def _legacy_entries(cfg: dict) -> list[dict]:
    """The pre-list configuration, expressed as a chain.

    Explicit portal choice → that one provider; explicit SERIN_AI_PROVIDER →
    same; auto → DeepSeek, then Anthropic, then the Claude CLI. Auto prefers
    DeepSeek on measured cost: benchmarked against the real briefing prompt it
    produced a briefing for roughly a fortieth of what claude-sonnet-4-6
    charged, in a third of the time.
    """
    requested = str(cfg.get("provider") or "").strip().lower()
    if requested in ("", "auto"):
        requested = settings.ai_provider.strip().lower()
    if requested not in ("", "auto"):
        return [{"id": requested}]
    return [{"id": "deepseek"}, {"id": "anthropic"}, {"id": "claude_cli"}]


def provider_chain() -> list[dict]:
    """The ordered, usable AI providers — the waterfall.

    Each entry: {id, kind, label, key, model, base_url, vision}. Order comes
    from the portal's provider list (drag to reorder); a provider missing its
    key simply drops out rather than failing the chain. Configs written
    before the list existed resolve through the legacy fields, so nothing
    breaks on upgrade.
    """
    cfg = _portal_config()
    raw = cfg.get("providers")
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    if not isinstance(raw, list) or not raw:
        raw = _legacy_entries(cfg)
    out: list[dict] = []
    seen: set[str] = set()
    for row in raw:
        if not isinstance(row, dict):
            continue
        entry = _resolve_entry(cfg, row)
        if entry and entry["id"] not in seen:
            seen.add(entry["id"])
            out.append(entry)
    return out


def vision_chain() -> list[dict]:
    """The waterfall, restricted to providers that can read images."""
    return [e for e in provider_chain() if e["vision"]]


def resolved_provider() -> str:
    """Head of the chain, in the vocabulary the status surfaces have always
    used ("anthropic_api" | "deepseek" | "claude_cli" | ... | "none")."""
    chain = provider_chain()
    if not chain:
        return "none"
    head = chain[0]["id"]
    return "anthropic_api" if head == "anthropic" else head

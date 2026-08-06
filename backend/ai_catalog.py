"""The catalog of AI providers Serin knows how to call.

One entry per provider the briefing/import stack can talk to. Everything but
Anthropic and the Claude CLI speaks the OpenAI chat-completions dialect, so a
catalog entry is mostly an id, a base URL, and a default model — the generic
client does the rest. The catalog is data, deliberately: the connector portal
renders "Add AI provider" straight from it, and adding a provider here is the
whole job.

Default models are exactly that — defaults. Model names rot faster than
releases ship, so every chain entry carries a user-editable model and these
values are only what the input is pre-filled with.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    label: str
    # "anthropic" (native Messages API), "openai" (chat-completions dialect),
    # or "claude_cli" (local binary).
    kind: str
    key_field: str = ""       # connector-config field holding the API key
    env_var: str = ""         # deployment-level fallback for that key
    base_url: str = ""        # OpenAI-dialect endpoint root ("" = native)
    default_model: str = ""
    vision: bool = False      # can it read screenshots (Smart Import)?
    needs_key: bool = True    # Ollama and the CLI authenticate differently
    help: str = ""


CATALOG: list[ProviderSpec] = [
    ProviderSpec(
        id="anthropic",
        label="Anthropic (Claude)",
        kind="anthropic",
        key_field="anthropic_api_key",
        env_var="ANTHROPIC_API_KEY",
        default_model="claude-sonnet-4-6",
        vision=True,
        help="console.anthropic.com → API keys.",
    ),
    ProviderSpec(
        id="deepseek",
        label="DeepSeek",
        kind="openai",
        key_field="deepseek_api_key",
        env_var="DEEPSEEK_API_KEY",
        base_url="https://api.deepseek.com",
        default_model="deepseek-v4-flash",
        vision=False,
        help="platform.deepseek.com. Roughly 1/20th the cost of Claude Sonnet; no image input.",
    ),
    ProviderSpec(
        id="openai",
        label="OpenAI",
        kind="openai",
        key_field="openai_api_key",
        env_var="OPENAI_API_KEY",
        base_url="https://api.openai.com/v1",
        default_model="gpt-5-mini",
        vision=True,
        help="platform.openai.com → API keys.",
    ),
    ProviderSpec(
        id="gemini",
        label="Google Gemini",
        kind="openai",
        key_field="gemini_api_key",
        env_var="GEMINI_API_KEY",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        default_model="gemini-2.5-flash",
        vision=True,
        help="aistudio.google.com → Get API key. Uses Google's OpenAI-compatible endpoint.",
    ),
    ProviderSpec(
        id="xai",
        label="xAI (Grok)",
        kind="openai",
        key_field="xai_api_key",
        env_var="XAI_API_KEY",
        base_url="https://api.x.ai/v1",
        default_model="grok-4",
        vision=True,
        help="console.x.ai → API keys.",
    ),
    ProviderSpec(
        id="openrouter",
        label="OpenRouter",
        kind="openai",
        key_field="openrouter_api_key",
        env_var="OPENROUTER_API_KEY",
        base_url="https://openrouter.ai/api/v1",
        default_model="deepseek/deepseek-chat",
        vision=False,
        help="openrouter.ai → Keys. One key, any model — set the model to route.",
    ),
    ProviderSpec(
        id="ollama",
        label="Ollama (local)",
        kind="openai",
        base_url="http://localhost:11434/v1",
        default_model="llama3.3",
        vision=False,
        needs_key=False,
        help="Runs on your own machine — nothing leaves it. Override the URL if Ollama is elsewhere.",
    ),
    ProviderSpec(
        id="claude_cli",
        label="Claude CLI (local dev)",
        kind="claude_cli",
        default_model="claude-sonnet-4-6",
        vision=False,
        needs_key=False,
        help="Uses your local `claude` binary and its sign-in.",
    ),
]

_BY_ID = {spec.id: spec for spec in CATALOG}

# The old fixed provider select stored these values; the catalog speaks ids.
LEGACY_ALIASES = {"anthropic_api": "anthropic"}


def get(provider_id: str) -> ProviderSpec | None:
    pid = LEGACY_ALIASES.get(provider_id, provider_id)
    return _BY_ID.get(pid)


def as_options() -> list[dict]:
    """The catalog, shaped for the connector portal's provider picker."""
    return [
        {
            "value": spec.id,
            "label": spec.label,
            "key_field": spec.key_field,
            "needs_key": spec.needs_key,
            "default_model": spec.default_model,
            "base_url": spec.base_url,
            "vision": spec.vision,
            "help": spec.help,
        }
        for spec in CATALOG
    ]

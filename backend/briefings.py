from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from backend import ai_provider, db
from backend.ai_provider import (
    anthropic_available,
    deepseek_available,
    resolved_deepseek_key,
    resolved_provider,
)
from backend.config import settings
from backend.news import fetch_news

logger = logging.getLogger(__name__)

RECENT_TRANSACTION_DAYS = 7
RECENT_TRANSACTION_LIMIT = 25


def _recent_transactions() -> list[dict[str, Any]]:
    """Last week's transactions, newest first — gives the briefing concrete
    activity to narrate ("DIV from MSFT credited Tuesday") instead of only
    inferring from position deltas."""
    cutoff = (datetime.now(UTC) - timedelta(days=RECENT_TRANSACTION_DAYS)).date().isoformat()
    recent = [
        {
            "occurred_at": t.occurred_at[:10],
            "action": t.action,
            "symbol": t.symbol,
            "quantity": t.quantity,
            "price": t.price,
            "amount": t.amount,
            "broker": t.broker,
        }
        for t in db.list_transactions(limit=500)
        if t.occurred_at[:10] >= cutoff
    ]
    recent.sort(key=lambda item: item["occurred_at"], reverse=True)
    return recent[:RECENT_TRANSACTION_LIMIT]


def build_portfolio_snapshot() -> dict[str, Any]:
    summary = db.portfolio_summary()
    return {
        "captured_at": datetime.now(UTC).isoformat(),
        "total_value": summary.total_value,
        "total_cost": summary.total_cost,
        "total_gain": summary.total_gain,
        "total_gain_pct": summary.total_gain_pct,
        "cash_value": summary.cash_value,
        "broker_breakdown": summary.broker_breakdown,
        "sector_breakdown": summary.sector_breakdown,
        "recent_transactions": _recent_transactions(),
        "positions": [
            {
                "symbol": position.symbol,
                "name": position.name,
                "broker": position.broker,
                "asset_type": position.asset_type,
                "quantity": position.quantity,
                "average_cost": position.average_cost,
                "current_price": position.current_price,
                "market_value": position.market_value,
                "unrealized_gain": position.unrealized_gain,
                "unrealized_gain_pct": position.unrealized_gain_pct,
                "sector": position.sector,
                "updated_at": position.updated_at,
            }
            for position in summary.positions
        ],
    }


BRIEFING_STYLE_CONFIG: dict[str, dict[str, Any]] = {
    "operator": {
        "label": "Operator Brief",
        "description": "Structured daily review for what changed and what needs attention.",
        "word_limit": 550,
        "sections": [
            "# Daily Briefing - <UTC date>",
            "## Portfolio Context",
            "## Market Context",
            "## Watch Items",
            "## Risk Flags",
            "## Questions To Review",
            "## Summary",
        ],
        "rules": [
            "Use concise bullets where possible.",
            "Include at most 3 watch items and at most 3 risk flags.",
            "Each watch item should include the symbol, reason, and supporting number.",
            "Make operational data issues visible, especially stale prices, missing sectors, missing cost basis, and unpriced assets.",
        ],
    },
    "analyst": {
        "label": "Analyst Brief",
        "description": "Deeper context, themes, and portfolio interpretation without trade advice.",
        "word_limit": 800,
        "sections": [
            "# Daily Briefing - <UTC date>",
            "## Portfolio Context",
            "## Market Context",
            "## Exposure Themes",
            "## Watch Items",
            "## Risk Flags",
            "## Questions To Review",
            "## Summary",
        ],
        "rules": [
            "Explain the main portfolio themes across concentration, sector exposure, broker exposure, cash, and relevant news.",
            "Separate observed facts from interpretation by using language like 'The snapshot shows' and 'This may indicate'.",
            "Tie news to current holdings only when the connection is explicit in the provided news JSON.",
            "Include data-quality caveats when missing or stale data could change the interpretation.",
        ],
    },
    "executive": {
        "label": "Executive Brief",
        "description": "Fast 60-second summary with only the highest-signal items.",
        "word_limit": 300,
        "sections": [
            "# Daily Briefing - <UTC date>",
            "## Summary",
            "## Top Signals",
            "## Review Today",
        ],
        "rules": [
            "Be brief and skimmable.",
            "Use at most 4 top signals.",
            "Prefer one-line bullets with numbers.",
            "Only mention news if it is directly relevant to a current holding.",
        ],
    },
}


def normalize_briefing_style(style: str | None) -> str:
    value = (style or "operator").strip().lower()
    return value if value in BRIEFING_STYLE_CONFIG else "operator"


def build_briefing_prompt(snapshot: dict[str, Any], news: dict[str, Any], style: str = "operator") -> str:
    style_key = normalize_briefing_style(style)
    config = BRIEFING_STYLE_CONFIG[style_key]
    sections = "\n".join(config["sections"])
    style_rules = "\n".join(f"- {rule}" for rule in config["rules"])
    return (
        "You are Serin, an AI portfolio briefing assistant.\n"
        f"Briefing style: {config['label']} - {config['description']}\n"
        "Create a daily portfolio briefing that helps the user understand what changed, "
        "what deserves attention, and what data may be incomplete.\n\n"
        "Do not give personalized investment advice. Do not give financial, tax, legal, "
        "or investment advice. Do not recommend buying, selling, holding, rebalancing, "
        "or timing trades. Present observations, context, risk checks, and questions "
        "for the user to review.\n\n"
        "Prioritize, in order:\n"
        "1. largest portfolio weights and concentration risks\n"
        "2. largest dollar gains/losses and day moves\n"
        "3. recent account activity from recent_transactions (dividends credited, "
        "buys/sells, fees) — name the symbol, amount, and day\n"
        "4. ticker-specific news affecting current holdings\n"
        "5. stale, missing, or suspicious data\n"
        "6. cash balance and unclassified exposure\n"
        "7. upcoming risks or events mentioned in news\n\n"
        "Style-specific rules:\n"
        f"{style_rules}\n\n"
        "Use this exact markdown structure:\n"
        f"{sections}\n\n"
        f"Keep it under {config['word_limit']} words. Use specific numbers from the portfolio snapshot. "
        "If data is missing, say so plainly.\n\n"
        f"Portfolio snapshot JSON:\n{json.dumps(snapshot, indent=2, sort_keys=True)}\n\n"
        f"Market/news JSON:\n{json.dumps(news, indent=2, sort_keys=True)}"
    )


def extract_summary(markdown: str) -> str:
    lines = [line.strip() for line in markdown.splitlines()]
    for idx, line in enumerate(lines):
        if line.lower() == "## summary":
            for candidate in lines[idx + 1 :]:
                if candidate and not candidate.startswith("#"):
                    return candidate[:240]
    for line in lines:
        if line and not line.startswith("#"):
            return line[:240]
    return "Daily briefing ready"


# Estimated list prices in USD per million tokens (input, output), matched by
# model-name prefix. These are estimates for display only, not billing.
# Longest prefix first — matching is first-hit, so "deepseek" before
# "deepseek-v4-pro" would price the pro model as flash.
# Verified against each vendor's published rates 2026-08-01. DeepSeek doubles
# these 09:00-12:00 and 14:00-18:00 Beijing time, which the estimate ignores;
# it is a pre-flight sanity check, not an invoice.
MODEL_PRICING_PER_MTOK: list[tuple[str, float, float]] = [
    ("deepseek-v4-pro", 0.435, 0.87),
    ("deepseek", 0.14, 0.28),
    ("claude-haiku", 1.0, 5.0),
    ("claude-sonnet-5", 2.0, 10.0),  # introductory rate to 2026-08-31; 3.0/15.0 after
    ("claude-sonnet", 3.0, 15.0),
    ("claude-opus", 5.0, 25.0),
    ("claude", 3.0, 15.0),
]


def estimate_model_cost_usd(usage: dict[str, Any]) -> float:
    input_tokens = float(usage.get("input_tokens") or 0)
    output_tokens = float(usage.get("output_tokens") or 0)
    model = str(usage.get("model") or settings.ai_model)
    input_price, output_price = 3.0, 15.0
    for prefix, in_p, out_p in MODEL_PRICING_PER_MTOK:
        if model.startswith(prefix):
            input_price, output_price = in_p, out_p
            break
    return round((input_tokens * input_price + output_tokens * output_price) / 1_000_000, 6)


def estimate_briefing_cost() -> dict[str, Any]:
    """Expected model + cost for the next briefing run — the cost guard.

    Prefers the observed average of recent runs on the same model; falls back
    to list-price math on a typical run. Surfaced next to the Run button so a
    provider change (e.g. Auto upgrading to Sonnet) is never a silent 20×.
    """
    chain = ai_provider.provider_chain()
    if not chain:
        return {"provider": "none", "model": "", "estimated_cost_usd": None, "basis": "no provider configured"}
    provider = resolved_provider()
    model = chain[0]["model"]

    recent = [
        b for b in db.list_briefings(limit=30)
        if b.status == "done" and b.model == model and (b.model_cost_usd or 0) > 0
    ][:5]
    if recent:
        average = sum(b.model_cost_usd for b in recent) / len(recent)
        return {
            "provider": provider,
            "model": model,
            "estimated_cost_usd": round(average, 4),
            "basis": f"average of last {len(recent)} run{'s' if len(recent) != 1 else ''}",
        }

    input_price, output_price = 3.0, 15.0
    for prefix, in_p, out_p in MODEL_PRICING_PER_MTOK:
        if model.startswith(prefix):
            input_price, output_price = in_p, out_p
            break
    # Typical run: ~7k prompt tokens (snapshot + news), ~1.2k output.
    estimate = (7000 * input_price + 1200 * output_price) / 1_000_000
    return {
        "provider": provider,
        "model": model,
        "estimated_cost_usd": round(estimate, 4),
        "basis": "list price for a typical run",
    }


def friendly_model_error(exc: Exception) -> str:
    message = str(exc)
    if "Insufficient Balance" in message or " 402" in message:
        if resolved_provider() == "deepseek":
            return (
                "DeepSeek account has no credit. Add balance at "
                "https://platform.deepseek.com/top_up and run the briefing again."
            )
        return "The AI provider account has insufficient credit. Top up your account and try again."
    if (
        "authentication_error" in message
        or "Invalid authentication credentials" in message
        or "Authentication Fails" in message
        or "Failed to authenticate" in message
    ):
        provider = resolved_provider()
        if provider == "claude_cli":
            return (
                "Claude CLI authentication failed. Run `claude auth login --claudeai` "
                "to refresh your Claude subscription login. For a long-lived local "
                "Serin token, run `claude setup-token` and set CLAUDE_CODE_OAUTH_TOKEN "
                "in `.env`, then restart Serin."
            )
        if provider == "deepseek":
            return "DeepSeek API authentication failed. Check the key in the AI briefing connector (or DEEPSEEK_API_KEY in `.env`)."
        return "Anthropic API authentication failed. Check the key in the AI briefing connector (or ANTHROPIC_API_KEY in `.env`)."
    return f"{type(exc).__name__}: {message}"


async def run_daily_briefing(briefing_id: int, style: str = "operator") -> None:
    try:
        style = normalize_briefing_style(style)
        snapshot = build_portfolio_snapshot()
        snapshot["briefing_style"] = style
        tickers = [
            item["symbol"]
            for item in snapshot["positions"]
            if item["asset_type"] != "cash" and item["symbol"] != "CASH"
        ]
        market_news = await fetch_news(tickers)
        prompt = build_briefing_prompt(snapshot, market_news, style=style)
        markdown, usage = await call_model(prompt)
        db.finish_briefing(
            briefing_id,
            status="done",
            summary=extract_summary(markdown),
            output_markdown=markdown,
            model_cost_usd=estimate_model_cost_usd(usage),
        )
    except Exception as exc:
        db.finish_briefing(
            briefing_id,
            status="error",
            error=friendly_model_error(exc),
        )


SYSTEM_PROMPT = (
    "You produce portfolio briefings for Serin. "
    "You are precise, cautious, and explicit when data is missing. "
    "You do not give financial, tax, legal, or investment advice."
)

DEEPSEEK_MAX_TOKENS = 10000


async def call_model(prompt: str) -> tuple[str, dict[str, Any]]:
    """Run the prompt through the provider waterfall.

    The chain's order is the user's (drag-to-reorder in the connector
    portal); each provider that fails is logged and the next one tries, so a
    provider outage degrades to the second choice instead of a dead briefing.
    Only the last failure raises.
    """
    chain = ai_provider.provider_chain()
    if not chain:
        raise RuntimeError(
            "No AI provider configured. Add one in the AI briefing "
            "connector (Connectors tab), or set ANTHROPIC_API_KEY / DEEPSEEK_API_KEY."
        )
    last_error: Exception | None = None
    for entry in chain:
        try:
            if entry["kind"] == "claude_cli":
                return await call_claude_cli(prompt)
            if entry["kind"] == "anthropic":
                return await call_anthropic_api(prompt, model=entry["model"])
            return await call_openai_compat(entry, prompt)
        except Exception as exc:
            last_error = exc
            if entry is not chain[-1]:
                logger.warning(
                    "AI provider %s failed (%s) — trying %s next",
                    entry["id"], exc, chain[chain.index(entry) + 1]["id"],
                )
    raise last_error if last_error else RuntimeError("No AI provider produced a briefing.")


def _extract_openai_message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""


def parse_openai_chat_response(data: dict[str, Any], model: str, provider: str) -> tuple[str, dict[str, Any]]:
    """Extract text + normalized usage from an OpenAI-compatible chat response."""
    choices = data.get("choices") or []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice, dict) else {}
    if not isinstance(message, dict):
        message = {}
    text = _extract_openai_message_text(message)
    text = text.strip()
    if not text:
        details = []
        finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None
        if finish_reason:
            details.append(f"finish_reason={finish_reason}")
        if message.get("reasoning_content"):
            details.append("reasoning_content_present=true")
        suffix = f" ({'; '.join(details)})" if details else ""
        guidance = ""
        if finish_reason == "length":
            guidance = "; the provider exhausted its completion budget before final text"
        raise RuntimeError(f"{provider} response did not include final text{suffix}{guidance}")
    raw_usage = data.get("usage") or {}
    usage = {
        "input_tokens": raw_usage.get("prompt_tokens") or 0,
        "output_tokens": raw_usage.get("completion_tokens") or 0,
        "model": data.get("model") or model,
        "provider": provider,
    }
    return text, usage


async def call_openai_compat(entry: dict[str, Any], prompt: str) -> tuple[str, dict[str, Any]]:
    """One chat-completions call against any OpenAI-dialect provider.

    ``entry`` is a resolved chain row from ``ai_provider.provider_chain()`` —
    OpenAI, DeepSeek, Gemini, xAI, OpenRouter and Ollama all speak this
    dialect; only the base URL, key and model differ.
    """
    body: dict[str, Any] = {
        "model": entry["model"],
        "max_tokens": DEEPSEEK_MAX_TOKENS,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    }
    if entry["id"] == "deepseek" and entry["model"].startswith("deepseek-v4"):
        # v4 reasons unless told not to, and reasoning counts against
        # max_tokens. Measured on the real briefing prompt: with thinking left
        # on, flash spent an entire 4000-token budget deliberating and returned
        # nothing, while pro took 54s to produce a shorter briefing than it
        # writes in 18s with thinking off. Summarising data you have already
        # been handed is not what deliberation is for.
        body["thinking"] = {"type": "disabled"}

    headers = {"content-type": "application/json"}
    if entry["key"]:
        headers["Authorization"] = f"Bearer {entry['key']}"
    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(
            f"{entry['base_url'].rstrip('/')}/chat/completions",
            headers=headers,
            json=body,
        )
    if response.status_code >= 400:
        detail = response.text[:500]
        raise RuntimeError(f"{entry['label']} API returned {response.status_code}: {detail}")

    return parse_openai_chat_response(response.json(), entry["model"], entry["id"])


async def call_deepseek(prompt: str) -> tuple[str, dict[str, Any]]:
    """Kept for callers that predate the chain; DeepSeek via the generic path."""
    if not deepseek_available():
        raise RuntimeError(
            "DeepSeek API key is not configured — add it in the AI briefing "
            "connector or set DEEPSEEK_API_KEY."
        )
    entry = {
        "id": "deepseek",
        "label": "DeepSeek",
        "key": resolved_deepseek_key(),
        "model": settings.deepseek_model,
        "base_url": settings.deepseek_base_url,
    }
    return await call_openai_compat(entry, prompt)


async def call_claude_cli(prompt: str) -> tuple[str, dict[str, Any]]:
    claude_bin = shutil.which("claude")
    if not claude_bin:
        raise RuntimeError("Claude CLI is not installed or not in PATH")

    env = os.environ.copy()
    if settings.claude_code_oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = settings.claude_code_oauth_token

    def _run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [claude_bin, "--print", "--model", settings.anthropic_model, prompt],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )

    result = await asyncio.to_thread(_run)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:800]
        if (
            "authentication_error" in detail
            or "Invalid authentication credentials" in detail
            or "Failed to authenticate" in detail
        ):
            raise RuntimeError(detail or "Claude CLI authentication failed")
        raise RuntimeError(f"Claude CLI returned {result.returncode}: {detail or 'no output'}")
    text = (result.stdout or "").strip()
    if not text:
        raise RuntimeError("Claude CLI response was empty")
    return text, {"provider": "claude_cli"}


async def call_anthropic_api(prompt: str, model: str = "") -> tuple[str, dict[str, Any]]:
    if not anthropic_available():
        raise RuntimeError(
            "Anthropic API key is not configured — add it in the AI briefing "
            "connector or set ANTHROPIC_API_KEY."
        )

    body = {
        "model": model or settings.anthropic_model,
        # Room for a briefing *and* whatever the model thinks first. On models
        # from Sonnet 5 onward, omitting `thinking` runs adaptive thinking, and
        # max_tokens caps thinking plus response text together — so a budget
        # sized for the prose alone is spent before any arrives, and the call
        # raises "did not include text" below. Sonnet 4.6 and Haiku 4.5 don't
        # think unless asked, so they simply never approach this.
        "max_tokens": 4000,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": prompt}],
    }

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            f"{settings.anthropic_base_url.rstrip('/')}/v1/messages",
            headers=ai_provider.anthropic_headers(),
            json=body,
        )
    if response.status_code >= 400:
        detail = response.text[:500]
        raise RuntimeError(f"Anthropic API returned {response.status_code}: {detail}")

    data = response.json()
    text_parts = [
        block.get("text", "")
        for block in data.get("content", [])
        if block.get("type") == "text"
    ]
    text = "\n".join(part for part in text_parts if part).strip()
    if not text:
        usage_out = int((data.get("usage") or {}).get("output_tokens", 0) or 0)
        if data.get("stop_reason") == "max_tokens" or usage_out >= body["max_tokens"]:
            raise RuntimeError(
                f"{body['model']} used its entire {body['max_tokens']}-token budget "
                "without producing a briefing — on models that think by default, "
                "max_tokens covers the thinking too. Raise it, or set "
                'thinking={"type": "disabled"} for this model.'
            )
        raise RuntimeError("Anthropic response did not include text")
    usage = dict(data.get("usage") or {})
    usage.setdefault("model", data.get("model") or settings.anthropic_model)
    usage.setdefault("provider", "anthropic_api")
    return text, usage

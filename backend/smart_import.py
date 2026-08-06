"""Smart import — AI-extracted positions with mandatory review.

Accepts arbitrary text (CSV / pasted) or images (PNG / JPG / WEBP), routes to
the right AI provider/model, asks the model to return a strict JSON array of
positions, then runs deterministic server-side validation. The extract path
is idempotent and has **no side effects** — the user must confirm the parsed
rows via a separate bulk-insert endpoint before anything reaches the DB.

Provider routing
----------------
- text input    → DeepSeek (cheap) if configured, else Anthropic
- image input   → Anthropic Claude Haiku (vision). DeepSeek's hosted
  ``/chat/completions`` endpoint **rejects** ``image_url`` content blocks
  with a 400 — their open-weights vision models are self-hosted only,
  confirmed empirically 2026-06-30. Image uploads require an Anthropic
  key; the UI fails loudly if only DeepSeek is configured.

Cost reference (current list prices, ~3k input + 1k output):
    text  on v4-flash : ~$0.001 / import
    image on Haiku    : ~$0.008 / import

Privacy note: image inputs are forwarded to the configured cloud provider.
The frontend surfaces this on the upload zone — the backend just makes the
call.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from typing import Any

import httpx

from backend import ai_provider, db
from backend.ai_provider import (
    anthropic_available as _anthropic_available,
)
from backend.ai_provider import (
    deepseek_available as _deepseek_available,
)
from backend.ai_provider import (
    resolved_deepseek_key as _resolved_deepseek_key,
)
from backend.ai_provider import (
    resolved_provider as _resolved_provider,
)
from backend.config import settings

# --- Pricing reference (mirrors briefings.MODEL_PRICING_PER_MTOK) -----------
# Used to surface a tiny cost estimate to the user before they commit.
MODEL_PRICING_PER_MTOK: dict[str, tuple[float, float]] = {
    "deepseek-v4-flash": (0.14, 0.28),
    "deepseek-v4-pro": (1.74, 3.48),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-4-6": (3.0, 15.0),
}


SYSTEM_PROMPT = (
    "You are a financial data extractor. The user will paste or upload a "
    "broker statement, CSV, screenshot, or PDF describing portfolio "
    "positions. Read it carefully and return a single JSON object describing "
    "every position you can identify.\n\n"
    "OUTPUT FORMAT — return JSON ONLY, no prose, matching this schema:\n"
    "{\n"
    "  \"positions\": [\n"
    "    {\n"
    "      \"symbol\": \"AAPL\",            // ticker, uppercase, required\n"
    "      \"name\": \"Apple Inc\",          // optional company name\n"
    "      \"quantity\": 100,                // shares, required, > 0\n"
    "      \"average_cost\": 145.20,         // per-share cost basis if visible, else 0\n"
    "      \"current_price\": 192.50,        // per-share current price if visible, else 0\n"
    "      \"broker\": \"schwab\",           // broker name (lowercase, underscore), default 'manual'\n"
    "      \"asset_type\": \"stock\"         // stock | etf | crypto | cash | option, default 'stock'\n"
    "    }\n"
    "  ],\n"
    "  \"notes\": \"\"                       // optional one-line summary or caveat\n"
    "}\n\n"
    "Rules:\n"
    "- Output JSON only. No markdown fences, no prose, no commentary.\n"
    "- If a value is genuinely unknown, use 0 for numbers and \"\" for strings.\n"
    "- Never invent symbols, quantities, or prices. Skip rows you cannot read.\n"
    "- Cash balances: use symbol=\"CASH\", asset_type=\"cash\", quantity=<amount>, average_cost=1, current_price=1.\n"
    "- Crypto: use symbols like BTC, ETH (not BTC-USD). Asset_type=\"crypto\".\n"
)


# --- Provider routing --------------------------------------------------------


@dataclass
class _Provider:
    name: str   # "deepseek" | "anthropic_api"
    model: str
    supports_vision: bool


def _select_provider(has_image: bool) -> _Provider:
    """Pick the right provider/model for the input type.

    Image inputs always go to Anthropic — DeepSeek's hosted ``/chat/completions``
    endpoint rejects the OpenAI-style ``image_url`` content block with a 400
    ("unknown variant `image_url`, expected `text`"), regardless of the
    requested model. The third-party guides claiming DeepSeek vision API
    support describe their open-weights model (self-hosted), not the public
    cloud API. Confirmed empirically 2026-06-30.

    Text inputs honour the configured Serin AI provider for cost savings.
    """
    configured = _resolved_provider()  # portal-aware: "deepseek" | "anthropic_api" | ...

    if has_image:
        if _anthropic_available():
            return _Provider("anthropic_api", "claude-haiku-4-5", True)
        raise RuntimeError(
            "Image extraction needs an Anthropic API key — DeepSeek's hosted "
            "API doesn't accept image input. Add your Anthropic API key to "
            "the AI briefing connector (Connectors tab → AI daily briefing → "
            "Configure), or paste the content as text instead."
        )

    # Text path — use the cheap default when possible.
    if configured == "deepseek" and _deepseek_available():
        return _Provider("deepseek", "deepseek-v4-flash", False)
    if configured == "anthropic_api" and _anthropic_available():
        return _Provider("anthropic_api", "claude-haiku-4-5", False)
    if _deepseek_available():
        return _Provider("deepseek", "deepseek-v4-flash", False)
    if _anthropic_available():
        return _Provider("anthropic_api", "claude-haiku-4-5", False)
    raise RuntimeError(
        "Smart import needs an AI provider configured. Add a DeepSeek or "
        "Anthropic API key to the AI briefing connector (Connectors tab → "
        "AI daily briefing → Configure)."
    )


# --- Provider call paths -----------------------------------------------------


async def _call_deepseek(
    provider: _Provider,
    user_content: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    body: dict[str, Any] = {
        "model": provider.model,
        "max_tokens": 4000,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "response_format": {"type": "json_object"},
    }
    if provider.model.startswith("deepseek-v4"):
        # v4 reasons unless told not to, and reasoning counts against
        # max_tokens — measured burning a whole 4000-token budget without
        # emitting a character. Extraction is a transcription job with the
        # answer already in front of it, so the deliberation buys nothing and
        # costs the entire response.
        body["thinking"] = {"type": "disabled"}
    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(
            f"{settings.deepseek_base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {_resolved_deepseek_key()}",
                "content-type": "application/json",
            },
            json=body,
        )
    if response.status_code >= 400:
        raise RuntimeError(
            f"DeepSeek returned {response.status_code}: {response.text[:400]}"
        )
    data = response.json()
    choices = data.get("choices") or [{}]
    text = (choices[0].get("message") or {}).get("content") or ""
    usage = data.get("usage") or {}
    return text, {
        "input_tokens": usage.get("prompt_tokens") or 0,
        "output_tokens": usage.get("completion_tokens") or 0,
        "model": provider.model,
        "provider": "deepseek",
    }


async def _call_anthropic(
    provider: _Provider,
    user_content: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    body = {
        "model": provider.model,
        "max_tokens": 4000,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_content}],
    }
    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(
            f"{settings.anthropic_base_url.rstrip('/')}/v1/messages",
            headers=ai_provider.anthropic_headers(),
            json=body,
        )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Anthropic returned {response.status_code}: {response.text[:400]}"
        )
    data = response.json()
    text_parts = [
        block.get("text", "")
        for block in data.get("content", [])
        if block.get("type") == "text"
    ]
    text = "\n".join(part for part in text_parts if part).strip()
    usage = data.get("usage") or {}
    return text, {
        "input_tokens": usage.get("input_tokens") or 0,
        "output_tokens": usage.get("output_tokens") or 0,
        "model": provider.model,
        "provider": "anthropic_api",
    }


def _build_text_content(text: str, hint: str | None) -> list[dict[str, Any]]:
    instructions = (
        "Extract every portfolio position from the following content. "
        "Return JSON only.\n\n"
    )
    if hint:
        instructions += f"User hint: {hint}\n\n"
    return [{"type": "text", "text": f"{instructions}---\n{text}\n---"}]


def _build_image_content(
    image_bytes: bytes, mime_type: str, provider: _Provider, hint: str | None
) -> list[dict[str, Any]]:
    b64 = base64.b64encode(image_bytes).decode("ascii")
    instructions = (
        "The attached image is a screenshot or photo of portfolio positions "
        "(broker app, statement, spreadsheet, etc.). Read it carefully and "
        "extract every position. Return JSON only.\n"
    )
    if hint:
        instructions += f"\nUser hint: {hint}\n"

    if provider.name == "anthropic_api":
        # Anthropic format: {"type": "image", "source": {"type": "base64", ...}}
        return [
            {"type": "text", "text": instructions},
            {
                "type": "image",
                "source": {"type": "base64", "media_type": mime_type, "data": b64},
            },
        ]
    # OpenAI/DeepSeek format: {"type": "image_url", "image_url": {"url": "data:..."}}
    return [
        {"type": "text", "text": instructions},
        {
            "type": "image_url",
            "image_url": {"url": f"data:{mime_type};base64,{b64}"},
        },
    ]


# --- Parsing + validation ----------------------------------------------------


def _strip_fences(text: str) -> str:
    """Some models still wrap JSON in ```json ... ``` despite the prompt."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_response(text: str) -> dict[str, Any]:
    cleaned = _strip_fences(text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        # Last-ditch: try to find the first {...} block.
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if match:
            data = json.loads(match.group(0))
        else:
            raise RuntimeError(f"Model did not return valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Model did not return a JSON object")
    return data


_KNOWN_ASSET_TYPES = {"stock", "etf", "crypto", "cash", "option"}
_SUSPICIOUS_PRICE = 50_000.0  # per-share cost over this → flag as likely typo / option
_SUSPICIOUS_QTY = 1_000_000.0


def _normalize_row(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Coerce one model-output row into Serin's PositionIn shape."""
    if not isinstance(raw, dict):
        return None
    symbol = str(raw.get("symbol") or "").strip().upper()
    if not symbol or len(symbol) > 24:
        return None
    try:
        quantity = float(raw.get("quantity") or 0)
    except (TypeError, ValueError):
        quantity = 0.0
    try:
        average_cost = float(raw.get("average_cost") or 0)
    except (TypeError, ValueError):
        average_cost = 0.0
    try:
        current_price = float(raw.get("current_price") or 0)
    except (TypeError, ValueError):
        current_price = 0.0
    asset_type = str(raw.get("asset_type") or "stock").strip().lower()
    if asset_type not in _KNOWN_ASSET_TYPES:
        asset_type = "stock"
    broker = (
        str(raw.get("broker") or "manual").strip().lower().replace(" ", "_") or "manual"
    )
    name = str(raw.get("name") or symbol).strip()
    return {
        "symbol": symbol,
        "name": name,
        "broker": broker,
        "asset_type": asset_type,
        "quantity": max(quantity, 0.0),
        "average_cost": max(average_cost, 0.0),
        "current_price": max(current_price, 0.0),
        "sector": "",
    }


def _row_warnings(row: dict[str, Any], existing_keys: set[tuple[str, str, str]]) -> list[str]:
    warnings: list[str] = []
    if row["asset_type"] != "cash":
        if row["quantity"] <= 0:
            warnings.append("quantity is zero — confirm the row")
        if row["quantity"] > _SUSPICIOUS_QTY:
            warnings.append(f"quantity > {_SUSPICIOUS_QTY:,.0f} — looks unusual")
        if row["average_cost"] > _SUSPICIOUS_PRICE:
            warnings.append("average cost > $50k/share — verify (option or typo?)")
        if row["current_price"] > _SUSPICIOUS_PRICE:
            warnings.append("current price > $50k/share — verify")
    key = (row["symbol"], row["broker"], row["asset_type"])
    if key in existing_keys:
        warnings.append("duplicates an existing position — confirm overwrite")
    if row["asset_type"] == "stock" and not row["symbol"].replace(".", "").replace("-", "").isalnum():
        warnings.append("symbol looks non-standard")
    return warnings


def _cost_estimate(usage: dict[str, Any]) -> float:
    in_p, out_p = MODEL_PRICING_PER_MTOK.get(usage.get("model") or "", (0, 0))
    return round(
        (float(usage.get("input_tokens", 0)) * in_p
         + float(usage.get("output_tokens", 0)) * out_p) / 1_000_000,
        6,
    )


def _privacy_notice(provider: _Provider) -> str:
    label = {
        "deepseek": "DeepSeek",
        "anthropic_api": "Anthropic",
    }.get(provider.name, provider.name)
    return (
        f"This content will be sent to {label} ({provider.model}) for parsing. "
        "Crop or redact anything sensitive (account numbers, names, addresses) first."
    )


# --- Public entry points -----------------------------------------------------


async def extract(
    *,
    text: str | None = None,
    image_bytes: bytes | None = None,
    image_mime: str | None = None,
    hint: str | None = None,
) -> dict[str, Any]:
    """Run extraction. Returns ``{rows, warnings, provider, model, cost_usd,
    notice, raw}``. No DB writes."""
    if not text and not image_bytes:
        raise RuntimeError("Provide either text or image content.")
    has_image = image_bytes is not None
    provider = _select_provider(has_image=has_image)

    if has_image:
        if not image_mime:
            raise RuntimeError("Image upload requires a mime type")
        user_content = _build_image_content(image_bytes, image_mime, provider, hint)
    else:
        user_content = _build_text_content(text or "", hint)

    if provider.name == "deepseek":
        raw_text, usage = await _call_deepseek(provider, user_content)
    else:
        raw_text, usage = await _call_anthropic(provider, user_content)

    parsed = _parse_response(raw_text)
    raw_rows = parsed.get("positions") or []
    notes = str(parsed.get("notes") or "").strip()

    existing_keys: set[tuple[str, str, str]] = set()
    for position in db.list_positions():
        existing_keys.add((position.symbol, position.broker, position.asset_type))

    rows = []
    for raw in raw_rows:
        row = _normalize_row(raw)
        if row is None:
            continue
        row["warnings"] = _row_warnings(row, existing_keys)
        rows.append(row)

    return {
        "rows": rows,
        "row_count": len(rows),
        "notes": notes,
        "provider": provider.name,
        "model": provider.model,
        "cost_usd": _cost_estimate(usage),
        "notice": _privacy_notice(provider),
    }


def bulk_insert(rows: list[dict[str, Any]], *, replace: bool = False) -> dict[str, Any]:
    """Commit user-confirmed rows. Upserts on (symbol, broker, asset_type)
    when ``replace`` is True; otherwise creates new and skips duplicates."""
    from backend.models import PositionIn

    inserted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    existing_keys = {
        (p.symbol, p.broker, p.asset_type) for p in db.list_positions()
    }

    for raw in rows:
        try:
            position_in = PositionIn(
                symbol=str(raw.get("symbol") or ""),
                name=str(raw.get("name") or raw.get("symbol") or ""),
                broker=str(raw.get("broker") or "manual"),
                asset_type=str(raw.get("asset_type") or "stock"),
                quantity=float(raw.get("quantity") or 0),
                average_cost=float(raw.get("average_cost") or 0),
                current_price=float(raw.get("current_price") or 0),
                sector=str(raw.get("sector") or ""),
            )
        except Exception as exc:  # invalid input, skip with reason
            skipped.append({"raw": raw, "error": str(exc)})
            continue

        key = (position_in.symbol, position_in.broker, position_in.asset_type)
        if key in existing_keys and not replace:
            skipped.append({"raw": raw, "error": "duplicate"})
            continue
        if key in existing_keys and replace:
            saved = db.upsert_position(position_in)
        else:
            saved = db.create_position(position_in)
        inserted.append(saved.model_dump() if hasattr(saved, "model_dump") else saved.__dict__)

    return {
        "inserted": len(inserted),
        "skipped": len(skipped),
        "positions": inserted,
        "skip_details": skipped,
    }

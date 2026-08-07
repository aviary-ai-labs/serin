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

import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import httpx

from backend import ai_provider, db
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


# Extraction is a transcription job — the answer is already in front of the
# model — so when the user hasn't chosen a model explicitly, downgrade the
# provider's briefing default to a cheap one. An explicit choice always wins.
_IMPORT_MODEL_DEFAULTS = {
    "anthropic": "claude-haiku-4-5",
}


def _select_entries(has_image: bool) -> list[dict[str, Any]]:
    """The usable waterfall for this input type — extraction tries each in turn.

    Image inputs need a vision-capable provider (DeepSeek's hosted API rejects
    ``image_url`` blocks with a 400 — confirmed empirically 2026-06-30; the
    Claude CLI path has no image plumbing either). Text can use the whole
    chain.
    """
    chain = ai_provider.vision_chain() if has_image else ai_provider.provider_chain()
    if not chain:
        if has_image and ai_provider.provider_chain():
            raise RuntimeError(
                "None of your configured AI providers accepts image input. "
                "Add Anthropic, OpenAI, Gemini or Grok in the AI briefing "
                "connector (Connectors tab), or paste the content as text."
            )
        raise RuntimeError(
            "Smart import needs an AI provider configured. Add one in the "
            "AI briefing connector (Connectors tab → AI daily briefing → "
            "Configure)."
        )
    entries = []
    for raw in chain:
        entry = dict(raw)
        if not entry.get("model_explicit") and entry["id"] in _IMPORT_MODEL_DEFAULTS:
            entry["model"] = _IMPORT_MODEL_DEFAULTS[entry["id"]]
        entries.append(entry)
    return entries


_http_detail = ai_provider.http_error_detail


# --- Provider call paths -----------------------------------------------------


async def _call_openai_compat(
    entry: dict[str, Any],
    user_content: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    body: dict[str, Any] = {
        "model": entry["model"],
        "max_tokens": 4000,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "response_format": {"type": "json_object"},
    }
    if entry["id"] == "deepseek" and entry["model"].startswith("deepseek-v4"):
        # v4 reasons unless told not to, and reasoning counts against
        # max_tokens — measured burning a whole 4000-token budget without
        # emitting a character. Extraction is a transcription job with the
        # answer already in front of it, so the deliberation buys nothing and
        # costs the entire response.
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
        raise RuntimeError(_http_detail(entry["label"], response.status_code, response.text))
    data = response.json()
    choices = data.get("choices") or [{}]
    text = (choices[0].get("message") or {}).get("content") or ""
    usage = data.get("usage") or {}
    return text, {
        "input_tokens": usage.get("prompt_tokens") or 0,
        "output_tokens": usage.get("completion_tokens") or 0,
        "model": entry["model"],
        "provider": entry["id"],
    }


async def _call_anthropic(
    entry: dict[str, Any],
    user_content: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    body = {
        "model": entry["model"],
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
        raise RuntimeError(_http_detail("Anthropic", response.status_code, response.text))
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
        "model": entry["model"],
        "provider": "anthropic_api",
    }


async def _call_claude_cli(
    entry: dict[str, Any],
    text: str | None,
    images: list[tuple[bytes, str]],
    hint: str | None,
) -> tuple[str, dict[str, Any]]:
    """Extraction via the local `claude` binary — no API key, your sign-in.

    The CLI has no image argument, but it is an agent with file access: write
    the pages to a temp directory, run it there, and tell it to read them.
    The directory is deleted the moment the call returns.
    """
    claude_bin = shutil.which("claude")
    if not claude_bin:
        raise RuntimeError("Claude CLI is not installed or not in PATH")

    instructions = SYSTEM_PROMPT + "\n\n"
    if hint:
        instructions += f"User hint: {hint}\n\n"

    with tempfile.TemporaryDirectory(prefix="serin-import-") as tmpdir:
        if images:
            paths = []
            for index, (image_bytes, mime_type) in enumerate(images):
                suffix = ".png" if "png" in mime_type else ".jpg"
                path = Path(tmpdir) / f"page-{index + 1}{suffix}"
                path.write_bytes(image_bytes)
                paths.append(path.name)
            prompt = (
                f"{instructions}Read the image file{'s' if len(paths) > 1 else ''} "
                f"{', '.join(paths)} in the current directory — screenshots or "
                "statement pages of portfolio positions. Extract every position. "
                "Reply with ONLY the JSON object, no other text."
            )
        else:
            prompt = (
                f"{instructions}Extract every portfolio position from the following "
                f"content. Reply with ONLY the JSON object, no other text.\n---\n{text}\n---"
            )

        env = os.environ.copy()
        if settings.claude_code_oauth_token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = settings.claude_code_oauth_token

        def _run() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [claude_bin, "--print", "--model", entry["model"], prompt],
                capture_output=True,
                text=True,
                timeout=240,
                env=env,
                cwd=tmpdir,
            )

        result = await asyncio.to_thread(_run)

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:800]
        raise RuntimeError(f"Claude CLI returned {result.returncode}: {detail or 'no output'}")
    output = (result.stdout or "").strip()
    if not output:
        raise RuntimeError("Claude CLI response was empty")
    return output, {"provider": "claude_cli", "model": entry["model"]}


def _build_text_content(text: str, hint: str | None) -> list[dict[str, Any]]:
    instructions = (
        "Extract every portfolio position from the following content. "
        "Return JSON only.\n\n"
    )
    if hint:
        instructions += f"User hint: {hint}\n\n"
    return [{"type": "text", "text": f"{instructions}---\n{text}\n---"}]


def _build_image_content(
    images: list[tuple[bytes, str]], entry: dict[str, Any], hint: str | None
) -> list[dict[str, Any]]:
    plural = "images are" if len(images) > 1 else "image is"
    instructions = (
        f"The attached {plural} a screenshot, photo, or document pages of "
        "portfolio positions (broker app, statement, spreadsheet, etc.). Read "
        "them carefully and extract every position. Return JSON only.\n"
    )
    if hint:
        instructions += f"\nUser hint: {hint}\n"

    content: list[dict[str, Any]] = [{"type": "text", "text": instructions}]
    for image_bytes, mime_type in images:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        if entry["kind"] == "anthropic":
            content.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": mime_type, "data": b64},
                }
            )
        else:
            # OpenAI dialect: {"type": "image_url", "image_url": {"url": "data:..."}}
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{b64}"},
                }
            )
    return content


MAX_PDF_PAGES = 5


def _pdf_page_images(pdf_bytes: bytes) -> tuple[list[tuple[bytes, str]], int]:
    """Rasterize a PDF's first pages to PNGs.

    Local rendering, deliberately: it makes PDF import work with *any*
    vision-capable provider instead of only the ones with native PDF input,
    and nothing but pixels ever leaves the machine. Returns (images,
    total_pages) so the caller can say when a statement was truncated.
    """
    import io

    import pypdfium2 as pdfium

    try:
        doc = pdfium.PdfDocument(pdf_bytes)
    except Exception as exc:
        raise RuntimeError(
            "Could not read that PDF — it may be corrupt or password-protected."
        ) from exc
    try:
        total = len(doc)
        images: list[tuple[bytes, str]] = []
        for index in range(min(total, MAX_PDF_PAGES)):
            page = doc[index]
            pil_image = page.render(scale=2.0).to_pil()
            buffer = io.BytesIO()
            pil_image.save(buffer, format="PNG")
            images.append((buffer.getvalue(), "image/png"))
            page.close()
        return images, total
    except Exception as exc:
        raise RuntimeError(
            "Could not render that PDF — it may be corrupt or password-protected."
        ) from exc
    finally:
        doc.close()


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


def _privacy_notice(entry: dict[str, Any]) -> str:
    if entry["id"] == "ollama":
        return (
            f"This content is parsed by your local Ollama ({entry['model']}) — "
            "nothing leaves this machine."
        )
    if entry["id"] == "claude_cli":
        return (
            f"This content is parsed via your Claude CLI ({entry['model']}) — "
            "it goes to Anthropic under your own sign-in."
        )
    return (
        f"This content will be sent to {entry['label']} ({entry['model']}) for parsing. "
        "Crop or redact anything sensitive (account numbers, names, addresses) first."
    )


# --- Public entry points -----------------------------------------------------


async def extract(
    *,
    text: str | None = None,
    image_bytes: bytes | None = None,
    image_mime: str | None = None,
    pdf_bytes: bytes | None = None,
    hint: str | None = None,
) -> dict[str, Any]:
    """Run extraction. Returns ``{rows, warnings, provider, model, cost_usd,
    notice, raw}``. No DB writes."""
    if not text and not image_bytes and not pdf_bytes:
        raise RuntimeError("Provide text, an image, or a PDF to extract from.")
    truncation_note = ""
    images: list[tuple[bytes, str]] = []
    if pdf_bytes is not None:
        images, total_pages = _pdf_page_images(pdf_bytes)
        if total_pages > MAX_PDF_PAGES:
            truncation_note = (
                f"Only the first {MAX_PDF_PAGES} of {total_pages} PDF pages were read."
            )
    elif image_bytes is not None:
        if not image_mime:
            raise RuntimeError("Image upload requires a mime type")
        images = [(image_bytes, image_mime)]

    has_image = bool(images)
    entries = _select_entries(has_image=has_image)

    # The waterfall, same as briefings: a provider outage falls through to the
    # next capable one instead of failing the import. Content is rebuilt per
    # provider — image blocks are dialect-specific.
    raw_text = ""
    usage: dict[str, Any] = {}
    entry = entries[0]
    last_error: Exception | None = None
    for candidate in entries:
        entry = candidate
        try:
            if entry["kind"] == "claude_cli":
                raw_text, usage = await _call_claude_cli(entry, text, images, hint)
            elif entry["kind"] == "anthropic":
                user_content = _build_image_content(images, entry, hint) if has_image else _build_text_content(text or "", hint)
                raw_text, usage = await _call_anthropic(entry, user_content)
            else:
                user_content = _build_image_content(images, entry, hint) if has_image else _build_text_content(text or "", hint)
                raw_text, usage = await _call_openai_compat(entry, user_content)
            last_error = None
            break
        except Exception as exc:
            last_error = exc
    if last_error is not None:
        raise last_error if isinstance(last_error, RuntimeError) else RuntimeError(str(last_error))

    parsed = _parse_response(raw_text)
    raw_rows = parsed.get("positions") or []
    notes = str(parsed.get("notes") or "").strip()
    if truncation_note:
        notes = f"{truncation_note} {notes}".strip()

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
        "provider": "anthropic_api" if entry["id"] == "anthropic" else entry["id"],
        "model": entry["model"],
        "cost_usd": _cost_estimate(usage),
        "notice": _privacy_notice(entry),
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

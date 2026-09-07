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
import logging
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
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
    "      \"asset_type\": \"stock\",        // stock | etf | crypto | cash | option, default 'stock'\n"
    "      \"tax_lots\": [                   // only when dated lot rows are visible\n"
    "        {\n"
    "          \"quantity\": 25,              // shares in this lot\n"
    "          \"cost_basis\": 140.10,        // per-share price paid, not total cost\n"
    "          \"acquired_at\": \"2025-12-18\" // purchase date, YYYY-MM-DD\n"
    "        }\n"
    "      ]\n"
    "    }\n"
    "  ],\n"
    "  \"transactions\": [\n"
    "    {\n"
    "      \"action\": \"sell\",            // buy | sell | dividend | interest | fee | tax |\n"
    "                                       //   deposit | withdrawal | transfer\n"
    "      \"symbol\": \"NFLX\",            // ticker for buy/sell/dividend; \"\" for cash rows\n"
    "      \"quantity\": 50,                 // shares for buy/sell; 0 otherwise\n"
    "      \"price\": 612.40,                // per-share price for buy/sell; the CASH AMOUNT\n"
    "                                       //   for dividend/fee/tax/deposit/withdrawal\n"
    "      \"fee\": 0,                       // commission or charge on this row\n"
    "      \"occurred_at\": \"2026-02-11\",  // trade/settlement date, YYYY-MM-DD, required\n"
    "      \"broker\": \"schwab\",\n"
    "      \"external_id\": \"\"             // broker's own reference for this row, if shown\n"
    "    }\n"
    "  ],\n"
    "  \"notes\": \"\"                       // optional one-line summary or caveat\n"
    "}\n\n"
    "Rules:\n"
    "- Output JSON only. No markdown fences, no prose, no commentary.\n"
    "- If a value is genuinely unknown, use 0 for numbers and \"\" for strings.\n"
    "- Never invent symbols, quantities, or prices. Skip rows you cannot read.\n"
    "- TAX LOT VIEWS: return exactly ONE aggregate position per symbol and broker. "
    "Use the summary row for the position quantity, average cost, and current price. "
    "Put each dated subrow under that position's tax_lots array; never return each "
    "lot as a separate position.\n"
    "- For tax lots, acquired_at is the acquisition/purchase date normalized to "
    "YYYY-MM-DD, quantity is that lot's shares, and cost_basis is the per-share "
    "Price Paid. Never invent a date or infer a lot from an undated row.\n"
    "- Cash balances: use symbol=\"CASH\", asset_type=\"cash\", quantity=<amount>, average_cost=1, current_price=1.\n"
    "- Crypto: use symbols like BTC, ETH (not BTC-USD). Asset_type=\"crypto\".\n"
    "- ACTIVITY / TRADE HISTORY / REALIZED GAIN statements: fill the transactions "
    "array. This is the only evidence that can establish a CLOSED position — a "
    "current holdings or tax-lot screen shows only what is still owned, so never "
    "infer a sale from one.\n"
    "- Cash movements matter as much as trades. Deposits, withdrawals, transfers, "
    "dividends, interest, fees and taxes all belong in transactions. Without them "
    "money added to the account is indistinguishable from money earned.\n"
    "- Classify carefully: a DEPOSIT is money arriving from outside; a TRANSFER "
    "moves money between two of this person's own accounts. Do not guess — if a "
    "row is ambiguous, still return it and describe the doubt in notes.\n"
    "- For non-share rows (dividend, fee, tax, deposit, withdrawal) put the cash "
    "amount in price and leave quantity 0.\n"
    "- external_id: copy the broker's own transaction reference when one is "
    "visible. It is what stops a re-imported statement from duplicating.\n"
    "- A statement may contain only positions, only transactions, or both. Return "
    "empty arrays rather than inventing either.\n"
)


logger = logging.getLogger(__name__)


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


def _float_value(value: Any) -> float:
    try:
        return max(float(value or 0), 0.0)
    except (TypeError, ValueError):
        return 0.0


def _normalize_lot_date(value: Any) -> str:
    """Normalize common brokerage dates while preserving invalid text for review."""
    text = str(value or "").strip()
    if not text:
        return ""
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt).date().isoformat()
        except ValueError:
            continue
    # Month-name forms, which is what a brokerage app screen usually shows:
    # "Aug 19, 2026", "August 19 2026", "19 Aug 2026". Tried on the whole
    # string rather than the first ten characters, since these are longer.
    cleaned = " ".join(text.replace(",", " ").split())
    for fmt in ("%b %d %Y", "%B %d %Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(cleaned, fmt).date().isoformat()
        except ValueError:
            continue
    return text


def _normalize_tax_lot(raw: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    acquired_at = _normalize_lot_date(
        raw.get("acquired_at")
        or raw.get("acquisition_date")
        or raw.get("purchase_date")
        or raw.get("date")
    )
    return {
        "quantity": _float_value(raw.get("quantity") or raw.get("qty") or raw.get("shares")),
        "cost_basis": _float_value(
            raw.get("cost_basis") or raw.get("price_paid") or raw.get("average_cost")
        ),
        "acquired_at": acquired_at,
    }


_KNOWN_ACTIONS = {
    "buy", "sell", "dividend", "interest", "fee", "tax",
    "deposit", "withdrawal", "transfer", "fx", "split", "adjustment",
}

#: What a model is likely to call each action if it does not use our word.
_ACTION_SYNONYMS = {
    "purchase": "buy", "bought": "buy", "b": "buy",
    "sale": "sell", "sold": "sell", "s": "sell",
    "div": "dividend", "dividends": "dividend", "reinvestment": "dividend",
    "int": "interest",
    "commission": "fee", "charge": "fee", "expense": "fee",
    "withholding": "tax", "tax_withheld": "tax",
    "contribution": "deposit", "funding": "deposit", "ach_in": "deposit",
    "distribution": "withdrawal", "ach_out": "withdrawal", "redemption": "withdrawal",
    "journal": "transfer", "internal_transfer": "transfer",
}


_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _canonical_action_word(raw: Any) -> str:
    """Reduce a broker's phrasing to one of Serin's actions.

    Brokerage screens name the order type as well as the direction — "limit
    buy", "market sell", "stop limit buy" — and matching the whole phrase
    dropped every row from a Robinhood history screenshot. So try the phrase
    first, then look for a known action word inside it.

    Word-wise rather than substring: "sell" appears inside nothing dangerous,
    but scanning for "buy" as a substring would match "buyback" and worse.
    """
    text = str(raw or "").strip().lower().replace("-", " ")
    squashed = text.replace(" ", "_")
    if squashed in _ACTION_SYNONYMS:
        return _ACTION_SYNONYMS[squashed]
    if squashed in _KNOWN_ACTIONS:
        return squashed
    words = [w for w in text.split() if w]
    for word in words:
        if word in _KNOWN_ACTIONS:
            return word
        if word in _ACTION_SYNONYMS:
            return _ACTION_SYNONYMS[word]
    return squashed


def _normalize_transaction(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Coerce one model-output row into Serin's TransactionIn shape.

    Returns None for anything undated: a transaction without a date cannot be
    placed in a return series, and guessing one would silently move somebody's
    performance to a day that never happened.
    """
    if not isinstance(raw, dict):
        return None
    action = _canonical_action_word(raw.get("action") or raw.get("type") or "")
    if action not in _KNOWN_ACTIONS:
        return None
    occurred_at = _normalize_lot_date(
        raw.get("occurred_at") or raw.get("date") or raw.get("trade_date")
        or raw.get("settlement_date")
    )
    if not occurred_at or not _ISO_DATE.match(occurred_at):
        # Unlike a tax lot, which a human reviews in a form, a transaction with
        # an unparsed date would be written straight into the ledger and then
        # silently misplace itself in every return series built from it.
        return None
    symbol = str(raw.get("symbol") or "").strip().upper()[:24]
    if action in ("buy", "sell") and not symbol:
        # A trade with no instrument cannot be replayed against a price series.
        return None
    quantity = abs(_float_value(raw.get("quantity") or raw.get("shares")))
    if action in ("buy", "sell") and quantity <= 0:
        # You cannot buy zero shares. Unparseable numbers coerce to 0, so
        # without this an unreadable row enters the ledger as a real trade of
        # nothing — and quietly shifts the share count it is replayed against.
        # Cash rows are exempt: a fee or deposit has no quantity by nature.
        return None
    price = abs(_float_value(raw.get("price") or raw.get("amount") or raw.get("value")))
    return {
        "action": action,
        "symbol": symbol,
        "quantity": quantity,
        "price": price,
        "fee": abs(_float_value(raw.get("fee") or raw.get("commission"))),
        "occurred_at": occurred_at,
        "broker": str(raw.get("broker") or "manual").strip().lower().replace(" ", "_") or "manual",
        # Carried through because a parsed broker export knows things a
        # screenshot does not: an option contract is priced per share but moves
        # 100x the cash, and storing it as a stock quietly breaks that.
        "asset_type": _canonical_asset_type(raw.get("asset_type")),
        "notes": str(raw.get("notes") or "").strip()[:200],
        "external_id": str(raw.get("external_id") or raw.get("reference") or "").strip()[:120],
    }


_ASSET_TYPES = {"stock", "etf", "crypto", "cash", "option"}


def _canonical_asset_type(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    return value if value in _ASSET_TYPES else "stock"


def _normalize_row(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Coerce one model-output row into Serin's PositionIn shape."""
    if not isinstance(raw, dict):
        return None
    symbol = str(raw.get("symbol") or "").strip().upper()
    if not symbol or len(symbol) > 24:
        return None
    quantity = _float_value(raw.get("quantity"))
    average_cost = _float_value(raw.get("average_cost"))
    current_price = _float_value(raw.get("current_price"))
    asset_type = str(raw.get("asset_type") or "stock").strip().lower()
    if asset_type not in _KNOWN_ASSET_TYPES:
        asset_type = "stock"
    broker = (
        str(raw.get("broker") or "manual").strip().lower().replace(" ", "_") or "manual"
    )
    name = str(raw.get("name") or symbol).strip()
    tax_lots = [
        lot
        for item in (raw.get("tax_lots") or [])
        if (lot := _normalize_tax_lot(item)) is not None
    ]
    # Tolerate older/provider-specific output that puts a date directly on a
    # lot-shaped position row. A later consolidation pass folds these rows
    # under their aggregate holding.
    if not tax_lots and any(
        raw.get(key) for key in ("acquired_at", "acquisition_date", "purchase_date")
    ):
        lot = _normalize_tax_lot(raw)
        if lot is not None:
            tax_lots.append(lot)

    return {
        "symbol": symbol,
        "name": name,
        "broker": broker,
        "asset_type": asset_type,
        "quantity": max(quantity, 0.0),
        "average_cost": max(average_cost, 0.0),
        "current_price": max(current_price, 0.0),
        "sector": "",
        "tax_lots": tax_lots,
    }


def _lot_key(lot: dict[str, Any]) -> tuple[float, float, str]:
    return (
        round(_float_value(lot.get("quantity")), 8),
        round(_float_value(lot.get("cost_basis")), 8),
        _normalize_lot_date(lot.get("acquired_at")),
    )


def _normalize_positions(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize model output and collapse lot-shaped duplicate positions."""
    rows = [
        row
        for raw in (parsed.get("positions") or [])
        if (row := _normalize_row(raw)) is not None
    ]

    # Some providers put lots at the top level. Attach them to the matching
    # holding before consolidation so both accepted JSON shapes behave alike.
    for raw_lot in parsed.get("tax_lots") or []:
        if not isinstance(raw_lot, dict):
            continue
        symbol = str(raw_lot.get("symbol") or "").strip().upper()
        broker = (
            str(raw_lot.get("broker") or "manual")
            .strip().lower().replace(" ", "_") or "manual"
        )
        lot = _normalize_tax_lot(raw_lot)
        if lot is None:
            continue
        for row in rows:
            if row["symbol"] == symbol and row["broker"] == broker:
                row["tax_lots"].append(lot)
                break

    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault((row["symbol"], row["broker"], row["asset_type"]), []).append(row)

    normalized: list[dict[str, Any]] = []
    for group in grouped.values():
        if len(group) == 1 or not any(row["tax_lots"] for row in group):
            normalized.extend(group)
            continue

        summary_rows = [row for row in group if not row["tax_lots"]]
        if summary_rows:
            aggregate = max(summary_rows, key=lambda row: row["quantity"])
        else:
            aggregate = dict(group[0])
            aggregate["quantity"] = sum(row["quantity"] for row in group)
            total_cost = sum(row["quantity"] * row["average_cost"] for row in group)
            aggregate["average_cost"] = (
                total_cost / aggregate["quantity"] if aggregate["quantity"] else 0.0
            )

        seen: set[tuple[float, float, str]] = set()
        lots: list[dict[str, Any]] = []
        for row in group:
            for lot in row["tax_lots"]:
                key = _lot_key(lot)
                if key not in seen:
                    seen.add(key)
                    lots.append(lot)
        aggregate = dict(aggregate)
        aggregate["tax_lots"] = lots
        normalized.append(aggregate)
    return normalized


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
    tax_lots = row.get("tax_lots") or []
    for index, lot in enumerate(tax_lots, start=1):
        if _float_value(lot.get("quantity")) <= 0:
            warnings.append(f"tax lot {index} needs a quantity")
        if _float_value(lot.get("cost_basis")) <= 0:
            warnings.append(f"tax lot {index} needs a per-share cost")
        acquired_at = _normalize_lot_date(lot.get("acquired_at"))
        try:
            datetime.strptime(acquired_at, "%Y-%m-%d")
        except ValueError:
            warnings.append(f"tax lot {index} needs a valid purchase date")
    if tax_lots and row["quantity"] > 0:
        lot_quantity = sum(_float_value(lot.get("quantity")) for lot in tax_lots)
        if abs(lot_quantity - row["quantity"]) > max(0.001, row["quantity"] * 0.000001):
            warnings.append(
                f"tax lots total {lot_quantity:g} shares, not {row['quantity']:g}"
            )
    return warnings


def _cost_estimate(usage: dict[str, Any]) -> float:
    in_p, out_p = MODEL_PRICING_PER_MTOK.get(usage.get("model") or "", (0, 0))
    return round(
        (float(usage.get("input_tokens", 0)) * in_p
         + float(usage.get("output_tokens", 0)) * out_p) / 1_000_000,
        6,
    )


def _privacy_notice(entry: dict[str, Any]) -> str:
    """Where this statement is about to go, in the user's terms.

    Deliberately says *whether it leaves the machine* and not which model or
    vendor parsed it. Naming the model turned a privacy disclosure into
    telemetry, and the destination is the part that actually matters to
    someone uploading a brokerage statement.
    """
    if entry["id"] == "ollama":
        return "This content is parsed on this machine — nothing leaves it."
    if entry["id"] == "claude_cli":
        return (
            "This content is parsed through the AI tool you signed in with on "
            "this machine."
        )
    return (
        "This content will be sent to the AI provider configured for this "
        "instance for parsing. Crop or redact anything sensitive (account "
        "numbers, names, addresses) first."
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
    """Run extraction. Returns ``{rows, transactions, notes, notice}``. No DB writes.

    The provider id, model name and cost estimate are deliberately absent: the
    import screen is for reviewing what was read out of a statement, and model
    telemetry there reads as an unfinished developer tool. They are still
    logged server-side, where operators need them.
    """
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
    notes = str(parsed.get("notes") or "").strip()
    if truncation_note:
        notes = f"{truncation_note} {notes}".strip()

    existing_keys: set[tuple[str, str, str]] = set()
    for position in db.list_positions():
        existing_keys.add((position.symbol, position.broker, position.asset_type))

    rows = []
    for row in _normalize_positions(parsed):
        row["warnings"] = _row_warnings(row, existing_keys)
        rows.append(row)

    transactions = [
        txn
        for raw_txn in (parsed.get("transactions") or [])
        if (txn := _normalize_transaction(raw_txn)) is not None
    ]

    logger.info(
        "smart import: provider=%s model=%s cost=%.4f rows=%d transactions=%d",
        entry["id"], entry["model"], _cost_estimate(usage), len(rows), len(transactions),
    )

    return {
        "rows": rows,
        "row_count": len(rows),
        "transactions": transactions,
        "transaction_count": len(transactions),
        "notes": notes,
        "notice": _privacy_notice(entry),
    }


def _transaction_fingerprint(txn: dict[str, Any]) -> str:
    """A stable id for an imported row, so the same statement lands once.

    The broker's own reference is used when it gave one. Otherwise the row is
    fingerprinted by what it *is* — date, action, instrument, size, price —
    which is stable across re-imports of the same statement and different for
    two genuinely separate trades. Two identical fills on the same day at the
    same price do collide, and are treated as one; that is the deliberate
    trade, because the alternative silently doubles somebody's contributions
    every time they re-upload a statement.
    """
    import hashlib

    reference = str(txn.get("external_id") or "").strip()
    broker = txn.get("broker", "manual")
    if reference:
        return f"{broker}:{reference}"[:120]
    seed = "|".join(
        str(txn.get(key, ""))
        for key in ("occurred_at", "action", "symbol", "quantity", "price", "fee", "broker")
    )
    return "fp:" + hashlib.sha256(seed.encode()).hexdigest()[:32]


def import_transactions(transactions: list[dict[str, Any]]) -> dict[str, Any]:
    """Commit reviewed transactions, skipping any already imported.

    Idempotency is enforced by the database's partial unique index rather than
    a read-then-write check here: two concurrent imports of the same statement
    would both pass a pre-check and both insert.
    """
    from backend import db
    from backend.models import TransactionIn

    inserted = skipped = 0
    for raw_txn in transactions:
        # Normalise here rather than trusting the caller. The extract path
        # already does it, but this endpoint takes whatever a client sends —
        # including rows a user edited by hand in the review table — and
        # TransactionIn's action is a closed set, so an unnormalised "limit
        # buy" would be silently skipped as a validation failure.
        txn = _normalize_transaction(raw_txn)
        if txn is None:
            skipped += 1
            continue
        fingerprint = _transaction_fingerprint(txn)
        try:
            row = TransactionIn(
                symbol=txn.get("symbol", ""),
                broker=txn.get("broker", "manual"),
                action=txn.get("action", "buy"),
                quantity=float(txn.get("quantity") or 0),
                price=float(txn.get("price") or 0),
                fee=float(txn.get("fee") or 0),
                asset_type=txn.get("asset_type", "stock"),
                notes=txn.get("notes", ""),
                occurred_at=txn["occurred_at"],
            )
        except Exception:
            skipped += 1
            continue
        created = db.create_transaction(row, source="import", external_id=fingerprint)
        if created is None:
            skipped += 1
        else:
            inserted += 1
    return {"inserted": inserted, "skipped": skipped}


def bulk_insert(rows: list[dict[str, Any]], *, replace: bool = False) -> dict[str, Any]:
    """Commit user-confirmed rows. Upserts on (symbol, broker, asset_type)
    when ``replace`` is True; otherwise creates new and skips duplicates."""
    from backend.models import PositionIn, TaxLotIn

    inserted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    tax_lots_inserted = 0
    tax_lots_skipped = 0
    tax_lot_skip_details: list[dict[str, Any]] = []
    existing_keys = {
        (p.symbol, p.broker, p.asset_type) for p in db.list_positions()
    }
    existing_lot_keys = {
        (lot.symbol, lot.broker, round(lot.quantity, 8), round(lot.cost_basis, 8), lot.acquired_at)
        for lot in db.list_tax_lots()
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
        elif key in existing_keys and replace:
            saved = db.upsert_position(position_in)
        else:
            saved = db.create_position(position_in)
        if key not in existing_keys or replace:
            inserted.append(saved.model_dump() if hasattr(saved, "model_dump") else saved.__dict__)
            existing_keys.add(key)

        for raw_lot in raw.get("tax_lots") or []:
            lot = _normalize_tax_lot(raw_lot)
            try:
                tax_lot_in = TaxLotIn(
                    symbol=position_in.symbol,
                    broker=position_in.broker,
                    quantity=lot["quantity"] if lot else 0,
                    cost_basis=lot["cost_basis"] if lot else 0,
                    acquired_at=lot["acquired_at"] if lot else "",
                )
                # TaxLotIn deliberately accepts arbitrary nonblank date text;
                # Smart Import is stricter because downstream calculations
                # require an ISO calendar date.
                datetime.strptime(tax_lot_in.acquired_at, "%Y-%m-%d")
            except Exception as exc:
                tax_lots_skipped += 1
                tax_lot_skip_details.append({"raw": raw_lot, "error": str(exc)})
                continue
            lot_key = (
                tax_lot_in.symbol,
                tax_lot_in.broker,
                round(tax_lot_in.quantity, 8),
                round(tax_lot_in.cost_basis, 8),
                tax_lot_in.acquired_at,
            )
            if lot_key in existing_lot_keys:
                tax_lots_skipped += 1
                tax_lot_skip_details.append({"raw": raw_lot, "error": "duplicate"})
                continue
            db.create_tax_lot(tax_lot_in)
            existing_lot_keys.add(lot_key)
            tax_lots_inserted += 1

    refreshed = 0
    if inserted:
        # The document was authoritative about WHAT the user holds, never
        # about what it's worth right now — statements and screenshots are as
        # old as whenever they were taken, and those prices sat on the rows
        # verbatim until someone happened to press Refresh. Re-price
        # immediately; cache-first, so on a shared deployment this is usually
        # a database read. Never let pricing fail the import that just worked.
        import logging

        from backend import prices

        try:
            refreshed = prices.refresh_prices(
                {str(row.get("symbol") or "") for row in inserted}
            ).get("updated", 0)
        except Exception:
            logging.getLogger(__name__).warning(
                "Post-import price refresh failed; imported prices kept", exc_info=True
            )

    return {
        "inserted": len(inserted),
        "skipped": len(skipped),
        "refreshed": refreshed,
        "positions": inserted,
        "skip_details": skipped,
        "tax_lots_inserted": tax_lots_inserted,
        "tax_lots_skipped": tax_lots_skipped,
        "tax_lot_skip_details": tax_lot_skip_details,
    }

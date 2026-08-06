#!/usr/bin/env python3
"""Run one real briefing prompt through several models and compare them.

The model behind daily briefings is the largest variable cost per subscriber
and the only AI output a subscriber actually reads, so it wants deciding on
evidence rather than on a price list. This sends the **production** prompt —
``build_briefing_prompt``, the same system prompt, the same portfolio snapshot
— to each model in turn, then writes every answer to its own file so they can
be read side by side.

    python -m scripts.bench_briefing                    # sample portfolio
    python -m scripts.bench_briefing --real             # your own database
    python -m scripts.bench_briefing --style analyst    # operator|analyst|executive
    python -m scripts.bench_briefing --models haiku,sonnet5

Costs are computed from the token counts each API actually returns, not from
an estimate of the prompt — an important difference, since the models tokenize
the same text differently and output length is what most of the spread comes
from.

Needs ``ANTHROPIC_API_KEY`` for the Claude models and ``OPENAI_API_KEY`` for
the GPT ones; whichever is missing is skipped with a note rather than failing
the run. Nothing here writes to your database.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from backend.briefings import (  # noqa: E402
    SYSTEM_PROMPT,
    build_briefing_prompt,
    build_portfolio_snapshot,
)

# Matches call_anthropic_api, so output length is comparable — and generous
# enough that a model which thinks before answering still reaches the answer.
# The first run of this harness used 1200, and both reasoning models spent the
# entire budget before writing a word: Sonnet 5 runs adaptive thinking when
# `thinking` is omitted, GPT-5 counts reasoning against max_completion_tokens,
# and neither is visible in the response except as an empty message. Haiku 4.5
# predates all of that and was the only one to produce anything, which made a
# broken run look like a clean sweep for the cheapest model.
MAX_TOKENS = 4000


@dataclass
class Model:
    key: str
    label: str
    api: str            # "anthropic" | "openai"
    model_id: str
    in_per_mtok: float
    out_per_mtok: float
    note: str = ""
    # Spends output budget reasoning unless told not to. Only these get a
    # thinking parameter — sending one to a model that predates the feature is
    # a 400, and Haiku 4.5 and Sonnet 4.6 simply don't think unless asked.
    thinks_by_default: bool = False
    # Per-entry override of --thinking, so "dspro" and "dspro+think" can sit in
    # the same table. Whether deliberation earns its cost is a question about
    # one model, not about the run, and answering it across two invocations
    # means comparing two tables and trusting nothing else drifted.
    thinking_mode: str = ""


# Prices per million tokens, list rates, verified 2026-08-01. Update alongside
# backend.briefings.MODEL_PRICING_PER_MTOK when they move.
MODELS: list[Model] = [
    Model("haiku", "Claude Haiku 4.5", "anthropic", "claude-haiku-4-5", 1.00, 5.00),
    Model(
        "sonnet5", "Claude Sonnet 5", "anthropic", "claude-sonnet-5", 2.00, 10.00,
        note="introductory pricing to 2026-08-31; $3/$15 after",
        thinks_by_default=True,
    ),
    Model("sonnet46", "Claude Sonnet 4.6", "anthropic", "claude-sonnet-4-6", 3.00, 15.00,
          note="what Serin runs today"),
    Model("gpt5mini", "GPT-5 mini", "openai", "gpt-5-mini", 0.25, 2.00,
          thinks_by_default=True),
    Model("gpt5nano", "GPT-5 nano", "openai", "gpt-5-nano", 0.05, 0.40,
          thinks_by_default=True),
    Model("dsflash", "DeepSeek v4 flash", "deepseek", "deepseek-v4-flash", 0.14, 0.28,
          note="Serin's configured DeepSeek model", thinks_by_default=True),
    Model("dspro", "DeepSeek v4 pro", "deepseek", "deepseek-v4-pro", 0.435, 0.87,
          thinks_by_default=True),
]
DEFAULT_KEYS = ["gpt5mini", "haiku", "sonnet5"]

# DeepSeek publishes 2x pricing during 09:00-12:00 and 14:00-18:00 Beijing
# time. The rates above are off-peak; a run inside those windows costs double
# what this reports, which is worth knowing before reading the column as a
# monthly forecast.
DEEPSEEK_PEAK_NOTE = "off-peak rates; DeepSeek charges 2x 09:00-12:00 and 14:00-18:00 Beijing time"


@dataclass
class Result:
    model: Model
    text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    error: str = ""
    words: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        self.words = len(self.text.split())

    @property
    def cost(self) -> float:
        return (
            self.input_tokens / 1_000_000 * self.model.in_per_mtok
            + self.output_tokens / 1_000_000 * self.model.out_per_mtok
        )

    def monthly(self, briefings: int = 30) -> float:
        """What one subscriber costs a month at one briefing a day — the number
        that actually bears on the subscription price."""
        return self.cost * briefings


# --- sample portfolio ------------------------------------------------------
# Deliberately awkward: a concentrated top holding, a loser, cash drag, two
# brokers holding the same symbol, an unclassified sector, and a stale price.
# A briefing that doesn't notice these is not earning its place in the product.

SAMPLE_SNAPSHOT: dict[str, Any] = {
    "captured_at": "2026-08-01T13:30:00+00:00",
    "total_value": 184_320.55,
    "total_cost": 151_900.00,
    "total_gain": 32_420.55,
    "total_gain_pct": 21.34,
    "cash_value": 41_200.00,
    "broker_breakdown": {"Schwab": 108_450.30, "Fidelity": 34_670.25, "manual": 41_200.00},
    "sector_breakdown": {
        "Technology": 96_180.40, "Healthcare": 21_300.10,
        "Financials": 14_240.05, "Unknown": 11_400.00,
    },
    "recent_transactions": [
        {"date": "2026-07-30", "symbol": "MSFT", "type": "dividend", "amount": 186.40},
        {"date": "2026-07-28", "symbol": "NVDA", "type": "sell", "quantity": 12, "amount": 14_880.00},
        {"date": "2026-07-22", "symbol": "VTI", "type": "buy", "quantity": 30, "amount": 8_940.00},
        {"date": "2026-07-19", "symbol": "—", "type": "fee", "amount": -24.00},
    ],
    "positions": [
        {"symbol": "NVDA", "name": "NVIDIA Corp", "broker": "Schwab", "asset_type": "stock",
         "quantity": 340, "average_cost": 118.20, "current_price": 172.40,
         "market_value": 58_616.00, "gain": 18_428.00, "gain_pct": 45.86,
         "day_change_pct": -3.10, "sector": "Technology", "currency": "USD",
         "price_updated_at": "2026-08-01T13:15:00+00:00"},
        {"symbol": "MSFT", "name": "Microsoft Corp", "broker": "Schwab", "asset_type": "stock",
         "quantity": 82, "average_cost": 372.10, "current_price": 458.90,
         "market_value": 37_629.80, "gain": 7_117.60, "gain_pct": 23.33,
         "day_change_pct": 0.42, "sector": "Technology", "currency": "USD",
         "price_updated_at": "2026-08-01T13:15:00+00:00"},
        {"symbol": "VTI", "name": "Vanguard Total Stock Market ETF", "broker": "Fidelity",
         "asset_type": "etf", "quantity": 96, "average_cost": 268.40,
         "current_price": 298.10, "market_value": 28_617.60, "gain": 2_851.20,
         "gain_pct": 11.06, "day_change_pct": 0.18, "sector": "Unknown",
         "currency": "USD", "price_updated_at": "2026-08-01T13:15:00+00:00"},
        {"symbol": "UNH", "name": "UnitedHealth Group", "broker": "Fidelity", "asset_type": "stock",
         "quantity": 44, "average_cost": 512.75, "current_price": 484.10,
         "market_value": 21_300.40, "gain": -1_260.60, "gain_pct": -5.59,
         "day_change_pct": -1.240, "sector": "Healthcare", "currency": "USD",
         "price_updated_at": "2026-08-01T13:15:00+00:00"},
        # Same symbol at a second broker — cross-broker overlap.
        {"symbol": "MSFT", "name": "Microsoft Corp", "broker": "Fidelity", "asset_type": "stock",
         "quantity": 12, "average_cost": 401.00, "current_price": 458.90,
         "market_value": 5_506.80, "gain": 694.80, "gain_pct": 14.44,
         "day_change_pct": 0.42, "sector": "Technology", "currency": "USD",
         "price_updated_at": "2026-08-01T13:15:00+00:00"},
        # Deliberately stale — four days without a price update.
        {"symbol": "SCHW", "name": "Charles Schwab Corp", "broker": "Schwab", "asset_type": "stock",
         "quantity": 190, "average_cost": 66.10, "current_price": 74.95,
         "market_value": 14_240.50, "gain": 1_681.50, "gain_pct": 13.39,
         "day_change_pct": 0.0, "sector": "Financials", "currency": "USD",
         "price_updated_at": "2026-07-28T20:00:00+00:00"},
        {"symbol": "CASH", "name": "Cash", "broker": "manual", "asset_type": "cash",
         "quantity": 41_200.00, "average_cost": 1.0, "current_price": 1.0,
         "market_value": 41_200.00, "gain": 0.0, "gain_pct": 0.0,
         "day_change_pct": 0.0, "sector": "Cash", "currency": "USD",
         "price_updated_at": "2026-08-01T13:15:00+00:00"},
    ],
}

SAMPLE_NEWS: dict[str, Any] = {
    "items": [
        {"title": "Nvidia slips as data-centre orders cool for a second quarter",
         "source": "Reuters", "published": "2026-08-01T11:02:00+00:00", "tickers": ["NVDA"]},
        {"title": "UnitedHealth under fresh scrutiny over billing practices",
         "source": "WSJ", "published": "2026-07-31T22:40:00+00:00", "tickers": ["UNH"]},
        {"title": "Microsoft raises quarterly dividend by 9%",
         "source": "CNBC", "published": "2026-07-30T13:00:00+00:00", "tickers": ["MSFT"]},
        {"title": "Treasury yields hit a nine-month high as inflation prints hot",
         "source": "Bloomberg", "published": "2026-08-01T09:15:00+00:00", "tickers": []},
    ]
}


def _flag_if_starved(result: Result, hit_cap: bool, reasoning_tokens: int = 0) -> Result:
    """Turn "spent the whole budget, said nothing" into an error.

    A model that thinks before answering can consume the entire token budget
    and return an empty message — no HTTP error, a full usage record, and a
    perfectly computable price. The first version of this script reported
    exactly that as a row in the results table: zero words, a confident cost,
    and the cheapest model apparently winning because it was the only one old
    enough not to think. A benchmark that prices failures is worse than no
    benchmark, so this refuses to score them.
    """
    if result.text.strip():
        return result
    detail = f"{result.output_tokens} output tokens"
    if reasoning_tokens:
        detail += f" ({reasoning_tokens} of them reasoning)"
    result.error = (
        f"produced no text after {detail}"
        + (" — hit the cap; raise --max-tokens" if hit_cap else "")
    )
    return result


async def call_anthropic(model: Model, prompt: str, thinking: str) -> Result:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return Result(model, error="ANTHROPIC_API_KEY not set")
    body: dict[str, Any] = {
        "model": model.model_id, "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": prompt}],
    }
    if model.thinks_by_default and thinking == "off":
        body["thinking"] = {"type": "disabled"}
    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=180) as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json=body,
        )
    elapsed = time.perf_counter() - started
    if response.status_code >= 400:
        return Result(model, error=f"HTTP {response.status_code}: {response.text[:200]}")
    data = response.json()
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    usage = data.get("usage") or {}
    result = Result(model, text, int(usage.get("input_tokens", 0)),
                    int(usage.get("output_tokens", 0)), elapsed)
    return _flag_if_starved(result, data.get("stop_reason") == "max_tokens")


async def call_openai(model: Model, prompt: str, thinking: str) -> Result:
    """OpenAI, and anything that speaks its chat-completions dialect.

    DeepSeek rides the same path — which is why Serin already parses its
    responses with parse_openai_chat_response — differing only in host, key,
    and the reasoning knob it doesn't have.
    """
    deepseek = model.api == "deepseek"
    env_var = "DEEPSEEK_API_KEY" if deepseek else "OPENAI_API_KEY"
    key = os.environ.get(env_var, "").strip()
    if not key:
        return Result(model, error=f"{env_var} not set")
    url = (
        f"{os.environ.get('DEEPSEEK_BASE_URL', 'https://api.deepseek.com').rstrip('/')}"
        "/chat/completions"
        if deepseek
        else "https://api.openai.com/v1/chat/completions"
    )
    # max_completion_tokens, not max_tokens: the newer models reject the old
    # name. Temperature left at the default — this is a comparison, and pinning
    # a value some models refuse would skew it.
    body: dict[str, Any] = {
        "model": model.model_id,
        # DeepSeek still takes the original name; the GPT-5 family rejects it.
        ("max_tokens" if deepseek else "max_completion_tokens"): MAX_TOKENS,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": prompt}],
    }
    if model.thinks_by_default:
        # Both vendors reason until the budget is gone and return an empty
        # message otherwise — GPT-5 mini and DeepSeek v4 flash each spent a
        # full 4000 tokens thinking without writing a word. Reasoning counts
        # against the output budget, so an unbounded effort level and a budget
        # sized for prose cannot both be satisfied.
        #
        # The knob differs. OpenAI has an effort level with a floor that emits
        # almost nothing. DeepSeek v4 thinks by default and reasoning_effort
        # alone will not stop it — only the explicit thinking block does, and
        # its effort scale has no minimum below "high".
        if deepseek:
            if thinking == "off":
                body["thinking"] = {"type": "disabled"}
            else:
                body["reasoning_effort"] = "high"
        else:
            body["reasoning_effort"] = "medium" if thinking == "on" else "minimal"
    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=180) as client:
        response = await client.post(
            url,
            headers={"Authorization": f"Bearer {key}", "content-type": "application/json"},
            json=body,
        )
    elapsed = time.perf_counter() - started
    if response.status_code >= 400:
        return Result(model, error=f"HTTP {response.status_code}: {response.text[:200]}")
    data = response.json()
    choices = data.get("choices") or [{}]
    text = ((choices[0].get("message") or {}).get("content")) or ""
    usage = data.get("usage") or {}
    result = Result(model, text, int(usage.get("prompt_tokens", 0)),
                    int(usage.get("completion_tokens", 0)), elapsed)
    reasoning = int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0) or 0)
    return _flag_if_starved(result, choices[0].get("finish_reason") == "length", reasoning)


async def run(models: list[Model], prompt: str, thinking: str) -> list[Result]:
    """Sequentially, not concurrently — the latency column is only meaningful
    if the runs aren't contending with each other."""
    results = []
    for model in models:
        print(f"  {model.label:22s} … ", end="", flush=True)
        # DeepSeek speaks the OpenAI dialect, so it shares that caller.
        caller = call_anthropic if model.api == "anthropic" else call_openai
        try:
            result = await caller(model, prompt, model.thinking_mode or thinking)
        except Exception as exc:  # noqa: BLE001 — one model failing must not end the run
            result = Result(model, error=f"{type(exc).__name__}: {exc}")
        print(result.error or f"{result.seconds:.1f}s, {result.output_tokens} out")
        results.append(result)
    return results


def report(results: list[Result], out_dir: Path, briefings_per_month: int) -> None:
    ok = [r for r in results if not r.error and r.text]
    print(f"\n{'model':22s} {'in':>7s} {'out':>6s} {'words':>6s} {'sec':>6s} "
          f"{'per run':>9s} {'per user/mo':>12s}")
    print("-" * 74)
    for r in results:
        if r.error:
            print(f"{r.model.label:22s} {r.error}")
            continue
        print(f"{r.model.label:22s} {r.input_tokens:7d} {r.output_tokens:6d} "
              f"{r.words:6d} {r.seconds:6.1f} ${r.cost:8.5f} ${r.monthly(briefings_per_month):11.2f}")

    if len(ok) > 1:
        cheapest = min(ok, key=lambda r: r.monthly(briefings_per_month))
        dearest = max(ok, key=lambda r: r.monthly(briefings_per_month))
        spread = dearest.monthly(briefings_per_month) - cheapest.monthly(briefings_per_month)
        print(f"\nSpread {cheapest.model.label} → {dearest.model.label}: "
              f"${spread:.2f} per subscriber per month.")
        print("Against ~$6.47 of contribution margin on an $8 Cloud subscription, "
              "that is the whole decision — so read the outputs before taking the cheap one.")

    for r in results:
        if r.model.note:
            print(f"note  {r.model.label}: {r.model.note}")
    if any(r.model.api == "deepseek" for r in results):
        print(f"note  DeepSeek: {DEEPSEEK_PEAK_NOTE}")

    out_dir.mkdir(parents=True, exist_ok=True)
    for r in ok:
        path = out_dir / f"{r.model.key}.md"
        path.write_text(
            f"# {r.model.label}\n\n"
            f"*{r.input_tokens} in / {r.output_tokens} out · {r.seconds:.1f}s · "
            f"${r.cost:.5f} per briefing · ${r.monthly(briefings_per_month):.2f} "
            f"per subscriber per month*\n\n---\n\n{r.text}\n",
            encoding="utf-8",
        )
    (out_dir / "summary.json").write_text(
        json.dumps(
            [{"model": r.model.model_id, "label": r.model.label, "error": r.error,
              "input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
              "words": r.words, "seconds": round(r.seconds, 2),
              "cost_per_run": round(r.cost, 6),
              "cost_per_user_month": round(r.monthly(briefings_per_month), 4)}
             for r in results],
            indent=2,
        ),
        encoding="utf-8",
    )
    if ok:
        print(f"\nWrote {len(ok)} briefing(s) to {out_dir}/ — read them before deciding.")
        print("The numbers rank cost. Only the prose ranks value.")


def main() -> int:
    global MAX_TOKENS  # --max-tokens rebinds it; the callers read it directly

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--models", default=",".join(DEFAULT_KEYS),
                        help=f"comma-separated: {', '.join(m.key for m in MODELS)}")
    parser.add_argument("--style", default="operator", help="operator | analyst | executive")
    parser.add_argument("--real", action="store_true",
                        help="use your own portfolio instead of the sample")
    parser.add_argument("--out", default="artifacts/briefing-bench")
    parser.add_argument("--max-tokens", type=int, default=MAX_TOKENS,
                        help="output budget; on models that think first this covers "
                             "the thinking too, so too low means no answer at all")
    parser.add_argument("--thinking", choices=("off", "on"), default="off",
                        help="off (default): disable thinking on models that do it by "
                             "default, so every model is compared on the briefing "
                             "rather than on how long it deliberates. on: let them "
                             "think, to see whether it is worth paying for here.")
    parser.add_argument("--briefings-per-month", type=int, default=30)
    args = parser.parse_args()

    by_key = {m.key: m for m in MODELS}
    chosen: list[Model] = []
    for raw in (k.strip() for k in args.models.split(",")):
        if not raw:
            continue
        # "dspro+think" is dspro with thinking forced on, so both can appear in
        # one table instead of across two runs.
        base_key, _, suffix = raw.partition("+")
        if base_key not in by_key:
            print(f"Unknown model: {base_key}", file=sys.stderr)
            thinkers = [k for k, m in by_key.items() if m.thinks_by_default]
            print(f"Available: {', '.join(by_key)}", file=sys.stderr)
            print(f"Append +think to: {', '.join(thinkers)}", file=sys.stderr)
            return 2
        if suffix and suffix != "think":
            print(f"Unknown suffix '+{suffix}' — only '+think' is understood", file=sys.stderr)
            return 2
        model = by_key[base_key]
        if suffix == "think":
            if not model.thinks_by_default:
                print(f"{model.label} has no thinking mode to switch on", file=sys.stderr)
                return 2
            model = replace(model, key=f"{model.key}-think",
                            label=f"{model.label} (thinking)", thinking_mode="on")
        chosen.append(model)
    MAX_TOKENS = args.max_tokens

    if args.real:
        snapshot, news = build_portfolio_snapshot(), {"items": []}
        if not snapshot.get("positions"):
            print("No positions in the database — run without --real to use the sample.",
                  file=sys.stderr)
            return 1
        print("Using your own portfolio. The outputs will contain real holdings.")
    else:
        snapshot, news = SAMPLE_SNAPSHOT, SAMPLE_NEWS

    prompt = build_briefing_prompt(snapshot, news, args.style)
    print(f"Prompt: {len(prompt):,} chars, style={args.style}, "
          f"{len(snapshot['positions'])} positions, max_tokens={args.max_tokens}")
    for model in chosen:
        if model.thinks_by_default:
            mode = model.thinking_mode or args.thinking
            print(f"  {model.label}: thinking {mode}")
    print()

    results = asyncio.run(run(chosen, prompt, args.thinking))
    report(results, Path(args.out), args.briefings_per_month)
    return 0 if any(not r.error for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

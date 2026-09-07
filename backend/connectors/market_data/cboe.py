"""Cboe market-data connector — keyless full-depth end-of-day history.

Cboe publishes delayed quotes and complete listed-lifetime daily OHLCV for US
equities/ETFs through its own public chart endpoint (the one powering
cboe.com's charts): ``cdn.cboe.com/api/global/delayed_quotes/charts/historical/
{SYMBOL}.json``. No key, served from a CDN, and — decisively — it covers
symbols other providers paywall per-symbol, at full depth (AAPL reaches back
to 2004; AFRM to its 2021 IPO).

One request returns the whole series; there is no range parameter, so periods
are sliced locally. Quotes come from the sibling ``quotes/{SYMBOL}.json``, which carries the
current delayed price and the time it was taken; history comes from the
chart endpoint above. Crypto stays on CoinGecko — Cboe answers "BTC" with a
listed equity trading around $35, and taking that for bitcoin would misprice
a holding by three orders of magnitude.
"""

from __future__ import annotations

import concurrent.futures
import logging
from datetime import UTC, date, datetime, timedelta

import httpx

from backend.connectors.base import (
    ConnectorManifest,
    MarketDataConnector,
    QuoteBudget,
    TestResult,
)
from backend.connectors.registry import register

logger = logging.getLogger(__name__)

_HISTORY = "https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/{symbol}.json"
#: The sibling endpoint, and the one a price sweep actually wants: a single
#: small object carrying the current delayed price and the time it was taken.
#: refresh_prices used to read the last row of the *historical* series instead,
#: which meant downloading a symbol's entire listed lifetime to learn one
#: number — and getting a daily close that could be two sessions old.
_QUOTE = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/{symbol}.json"

#: Concurrent quote requests. It is a CDN, and a book of five hundred priced
#: one at a time would take twenty seconds — longer than the interval the
#: sweep runs on.
_MAX_PARALLEL = 8
_TIMEOUT = 25.0
_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


def _cboe_symbol(symbol: str, asset_type: str) -> str | None:
    ticker = (symbol or "").strip().upper()
    if not ticker or asset_type == "crypto":
        return None  # crypto handled by the CoinGecko layer
    return ticker


def _period_start(period: str) -> date | None:
    today = datetime.now(UTC).date()
    p = (period or "").lower()
    days = {"1w": 7, "1m": 31, "3m": 93, "6m": 186, "1y": 366, "5y": 5 * 366}.get(p)
    if days:
        return today - timedelta(days=days)
    if p == "ytd":
        return date(today.year, 1, 1)
    return None  # "max" and anything unrecognised: the full series


#: Consecutive fully-throttled chunks after which a sweep gives up on Cboe for
#: this pass. Cboe's limiter is a bucket that refills over time, so requests
#: made while it is empty are not merely refused — they keep it empty. A book
#: that carries on asking turns a brief throttle into a sustained one, which is
#: how a sweep ends up pricing nothing at all rather than most of the book.
_GIVE_UP_AFTER_THROTTLED_CHUNKS = 2

_THROTTLED = "Cboe rate limit — sweep is asking faster than it allows"


def _in_parallel(items: list, work) -> list:
    """Map ``work`` over ``items``, order preserved, never raising.

    Results are indexed by request, never by arrival: completion order is
    arbitrary, and zipping by arrival would return real prices attached to the
    wrong symbols — a failure that looks entirely correct on screen.
    """
    if not items:
        return []
    results: list = [(None, "not attempted")] * len(items)
    throttled_chunks = 0

    # Chunked rather than one big submit, so there is a point at which the
    # sweep can notice it is being refused and stop.
    for start in range(0, len(items), _MAX_PARALLEL):
        chunk = items[start:start + _MAX_PARALLEL]
        with concurrent.futures.ThreadPoolExecutor(max_workers=_MAX_PARALLEL) as pool:
            futures = {pool.submit(work, item): start + i
                       for i, item in enumerate(chunk)}
            for future in concurrent.futures.as_completed(futures):
                index = futures[future]
                try:
                    results[index] = future.result()
                except Exception as exc:
                    results[index] = (None, f"Cboe request failed: {exc!r}")

        window = results[start:start + len(chunk)]
        if all(error == _THROTTLED for _price, error in window):
            throttled_chunks += 1
            if throttled_chunks >= _GIVE_UP_AFTER_THROTTLED_CHUNKS:
                for i in range(start + len(chunk), len(items)):
                    results[i] = (None, _THROTTLED)
                break
        else:
            throttled_chunks = 0
    return results


def _fetch_quote(cboe_symbol: str) -> tuple[float | None, str | None]:
    """The symbol's current delayed price, or why there isn't one."""
    try:
        resp = httpx.get(_QUOTE.format(symbol=cboe_symbol), headers=_HEADERS, timeout=_TIMEOUT)
        if resp.status_code == 429:
            # Cboe throttles, and a sweep that trips it must say so: "throttled"
            # and "not covered" call for opposite responses — wait and retry
            # versus stop asking. Reported as a plain failure, a whole book of
            # these reads as a book nothing can price.
            return None, _THROTTLED
        if resp.status_code == 403:
            # What Cboe returns for something it does not list at all — the
            # mutual funds in a book come back this way.
            return None, "not listed on Cboe"
        resp.raise_for_status()
        payload = resp.json()
    except httpx.HTTPError as exc:
        return None, f"Cboe request failed: {exc}"
    except ValueError:
        return None, "Cboe: unexpected response (not JSON)"

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return None, "Cboe: no quote in response"
    try:
        price = float(data.get("current_price") or 0)
    except (TypeError, ValueError):
        return None, "Cboe: unreadable price"
    # A zero is absence, not a price of nothing.
    return (price, None) if price > 0 else (None, "no Cboe price")


def _fetch_series(cboe_symbol: str) -> tuple[list[dict], str | None]:
    """The symbol's complete daily series, oldest first."""
    try:
        resp = httpx.get(_HISTORY.format(symbol=cboe_symbol), headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
    except httpx.HTTPError as exc:
        return [], f"Cboe request failed: {exc}"
    except ValueError:
        return [], "Cboe: unexpected response (not JSON)"
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        return [], "Cboe: no data for symbol"
    clean = [
        row for row in rows
        if isinstance(row, dict) and row.get("date") and float(row.get("close") or 0) > 0
    ]
    clean.sort(key=lambda row: str(row["date"]))
    return clean, None


@register
class CboeConnector(MarketDataConnector):
    manifest = ConnectorManifest(
        id="cboe",
        name="Cboe",
        kind="market_data",
        description=(
            "Free, keyless daily history for US stocks and ETFs from the Cboe exchange's "
            "public delayed-quotes feed — full listed-lifetime depth, including symbols "
            "other providers paywall. EOD closes, not live ticks."
        ),
        icon="ti-database",
        docs_url="https://www.cboe.com/delayed_quotes/",
        default_enabled=True,
        asset_scope="all",
        connect_method="none",  # keyless — nothing to configure
        config_schema=[],
    )

    #: Measured, not published — Cboe documents no limit, so this comes from
    #: probing the CDN directly. A steady 45 requests a minute ran clean for
    #: three minutes; 60 a minute came back 37% refused. The ceiling sits
    #: around fifty, so this claims forty and leaves the rest as margin: the
    #: cost of understating it is a slower sweep, and the cost of overstating
    #: it is a sweep that asks faster than Cboe answers and prices nothing.
    #:
    #: The limit is per-IP — a Fly machine priced a book clean while this
    #: developer's address was being refused in the same minute — so it is a
    #: budget per deployment rather than one shared across all of them.
    #:
    #: One symbol per request is the real constraint at scale: a 500-symbol
    #: book costs 500 requests, which lands on the 15-minute ceiling however
    #: generous the per-minute figure. Cboe is the keyless backstop, not the
    #: provider a large deployment should lean on.
    quote_budget = QuoteBudget(batch_size=1, per_minute=40)

    def refresh_prices(self, positions) -> dict:
        prices: dict[str, tuple[float, str]] = {}
        errors: list[str] = []
        seen: dict[str, object] = {}
        for position in positions:
            seen.setdefault(position.symbol, position)

        wanted: list[tuple[str, str]] = []
        for symbol, position in seen.items():
            cboe = _cboe_symbol(symbol, getattr(position, "asset_type", "stock"))
            if cboe is None:
                # Load-bearing. Cboe answers "BTC" with a listed equity trading
                # around $35; taking it for bitcoin would misprice the holding
                # by three orders of magnitude and look like a real number.
                errors.append(f"{symbol}: not covered by Cboe (crypto → CoinGecko)")
                continue
            wanted.append((symbol, cboe))

        for (symbol, _cboe), (price, error) in zip(
            wanted, _in_parallel([c for _s, c in wanted], _fetch_quote), strict=False
        ):
            if error or price is None:
                errors.append(f"{symbol}: {error or 'no Cboe price'}")
                continue
            prices[symbol] = (round(price, 6), "")
        return {"prices": prices, "errors": errors}

    def fetch_history(self, period, symbols, positions_by_symbol) -> dict:
        history: dict[str, dict[str, list]] = {}
        errors: list[str] = []
        start = _period_start(period)
        floor = start.isoformat() if start else ""
        for symbol in symbols:
            position = positions_by_symbol.get(symbol)
            cboe = _cboe_symbol(symbol, getattr(position, "asset_type", "stock") if position else "stock")
            if cboe is None:
                errors.append(f"{symbol}: not covered by Cboe (crypto → CoinGecko)")
                continue
            rows, error = _fetch_series(cboe)
            kept = [row for row in rows if str(row["date"]) >= floor] if floor else rows
            if error or len(kept) < 2:
                errors.append(f"{symbol}: {error or 'not enough Cboe history'}")
                continue
            history[symbol] = {
                "dates": [str(row["date"]) for row in kept],
                "closes": [round(float(row["close"]), 6) for row in kept],
            }
        return {"history": history, "errors": errors}

    def quote(self, symbol, asset_type) -> dict | None:
        cboe = _cboe_symbol(symbol, asset_type)
        if cboe is None:
            return None
        rows, error = _fetch_series(cboe)
        if error or not rows:
            return None
        last = rows[-1]
        # The series is the only source for the day's range and the year's, so
        # the detail view still needs it. The price, though, comes from the
        # quote endpoint when it answers: the last daily close can be two
        # sessions old, and a popup contradicting the dashboard's number for
        # the same holding reads as a bug in both.
        live, _live_error = _fetch_quote(cboe)
        prev_row = round(float(rows[-2]["close"]), 6) if len(rows) >= 2 else None
        close = round(float(last["close"]), 6)
        price = round(live, 6) if live else close
        # Against a live price the previous close is the last *settled* close,
        # not the one before it.
        prev = close if live else (prev_row if prev_row is not None else close)
        year_floor = (datetime.now(UTC).date() - timedelta(days=366)).isoformat()
        year = [float(row["close"]) for row in rows if str(row["date"]) >= year_floor] or [price]
        day_change = price - prev
        return {
            "symbol": symbol,
            "name": symbol,
            "price": price,
            "previous_close": prev,
            "day_change": round(day_change, 6),
            "day_change_pct": round((day_change / prev * 100) if prev > 0 else 0.0, 4),
            "day_high": round(float(last.get("high") or 0), 6),
            "day_low": round(float(last.get("low") or 0), 6),
            "year_high": round(max(year), 6),
            "year_low": round(min(year), 6),
            "volume": float(last.get("volume") or 0),
            "market_cap": 0.0,
            "sector": "",
            "currency": "USD",
            "provider": "cboe",
        }

    def test(self) -> TestResult:
        rows, error = _fetch_series("AAPL")
        if error or not rows:
            return TestResult(ok=False, message=error or "Could not reach Cboe.")
        last = rows[-1]
        return TestResult(
            ok=True,
            message=f"Reached Cboe — {len(rows)} days of AAPL, last close {float(last['close']):.2f} on {last['date']}.",
        )

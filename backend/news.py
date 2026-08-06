from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import httpx

FEEDS = [
    {
        "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
        "source": "MarketWatch",
    },
    {
        "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
        "source": "CNBC",
    },
]

CACHE_TTL_SECONDS = 300
_cache: dict = {"items": [], "fetched_at": 0.0}


def _parse_pub_date(raw: str) -> str:
    if not raw:
        return ""
    try:
        return parsedate_to_datetime(raw).isoformat()
    except Exception:
        return raw


def _parse_rss(xml_text: str, source: str) -> list[dict]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    items: list[dict] = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        summary = re.sub(r"<[^>]+>", "", item.findtext("description") or "").strip()
        items.append(
            {
                "title": title,
                "link": (item.findtext("link") or "").strip(),
                "source": source,
                "published": _parse_pub_date(item.findtext("pubDate") or ""),
                "summary": summary[:280],
            }
        )
    return items


def match_portfolio_news(items: list[dict], tickers: list[str]) -> list[dict]:
    """Match headlines to tickers as standalone uppercase words.

    Case-sensitive on purpose: the English word "Now" must not match the
    ticker NOW, and "Robinhood" must not match HOOD.
    """
    patterns = {
        ticker: re.compile(rf"(?<![A-Za-z0-9]){re.escape(ticker)}(?![A-Za-z0-9])")
        for ticker in {t.upper() for t in tickers}
    }
    matched: list[dict] = []
    for item in items:
        text = f"{item.get('title', '')} {item.get('summary', '')}"
        for ticker, pattern in patterns.items():
            if pattern.search(text):
                matched.append({**item, "matched_ticker": ticker})
                break
    return matched


async def fetch_news(tickers: list[str] | None = None) -> dict:
    now = time.time()
    if now - _cache["fetched_at"] < CACHE_TTL_SECONDS and _cache["items"]:
        all_items = _cache["items"]
    else:
        all_items = []
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            for feed in FEEDS:
                try:
                    response = await client.get(
                        feed["url"],
                        headers={
                            "User-Agent": "serin-local/0.1",
                            "Accept": "application/rss+xml, application/xml, text/xml",
                        },
                    )
                    if response.status_code == 200:
                        all_items.extend(_parse_rss(response.text, feed["source"])[:12])
                except Exception:
                    continue

        seen: set[str] = set()
        deduped: list[dict] = []
        for item in all_items:
            key = item["title"].lower()[:80]
            if key not in seen:
                seen.add(key)
                deduped.append(item)
        deduped.sort(key=lambda item: item.get("published", ""), reverse=True)
        all_items = deduped
        _cache["items"] = all_items
        _cache["fetched_at"] = now

    portfolio_news = match_portfolio_news(all_items, tickers or [])

    return {
        "portfolio_news": portfolio_news[:10],
        "market_news": all_items[:20],
        "fetched_at": _cache["fetched_at"],
    }

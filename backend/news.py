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


#: Corporate furniture that carries no identity. Stripped from a holding's
#: name to leave the part a journalist would actually write.
_NAME_NOISE = re.compile(
    r"\b("
    r"inc|corp|corporation|incorporated|company|co|ltd|limited|plc|llc|"
    r"holdings?|group|technologies|international|"
    r"adr|ads|sponsored|class\s+[a-c]|common\s+stock|ordinary\s+shares|"
    r"the"
    r")\b\.?",
    re.IGNORECASE,
)

#: Issuers and wrappers whose names belong to hundreds of products. "Fidelity"
#: matched an article about 401(k) millionaires and "State Street" one about
#: snowbirds; neither was about a holding.
_NOT_A_COMPANY = {
    "fidelity", "vanguard", "schwab", "ishares", "proshares", "spdr",
    "invesco", "state street", "direxion", "global x", "first trust",
    "jpmorgan", "blackrock", "pimco", "franklin", "t rowe price",
}

#: A name containing one of these describes a fund rather than a company, and
#: funds are not what a headline is ever about. Ticker matching still applies.
#: Matched on word boundaries, not as substrings — "etf" sits inside "Netflix",
#: which silently disqualified the one holding the feeds talk about most.
_FUND_WORDS = re.compile(
    r"\b(etf|fund|trust|index|portfolio|money\s+market|shares|strategy)\b",
    re.IGNORECASE,
)

#: Below this a name is an initialism or a common word, and matching it does
#: more harm than the headline it finds is worth.
_MIN_ALIAS = 4


def company_alias(name: str) -> str | None:
    """The part of a holding's name a headline would actually use.

    "Alphabet Inc. Class C Common Stock" is never how a story refers to it;
    "Alphabet" is. Feeds write company names and almost never tickers, which
    is why a ticker-only matcher found nothing in twenty MarketWatch and CNBC
    headlines while three of them were about holdings.

    Returns None when what is left cannot safely be matched: a fund, an
    issuer's own name, or something too short to be more than an initialism.
    """
    if _FUND_WORDS.search(name or ""):
        return None
    core = _NAME_NOISE.sub(" ", name or "")
    # Punctuation only where it separates rather than spells: the dot in
    # "JD.com" is part of the name, the one after "Inc" is not.
    core = re.sub(r"(?<![A-Za-z0-9])[.,]|[.,](?![A-Za-z0-9])", " ", core)
    core = re.sub(r"\s+", " ", core).strip()
    if len(core) < _MIN_ALIAS or core.lower() in _NOT_A_COMPANY:
        return None
    return core


def match_portfolio_news(items: list[dict], tickers: list[str],
                         names: dict[str, str] | None = None) -> list[dict]:
    """Match headlines to holdings by ticker or by company name.

    Tickers stay case-sensitive: the English word "Now" must not match the
    ticker NOW, and "Robinhood" must not match HOOD. Names are matched
    case-insensitively as whole phrases, because a headline writes "Netflix"
    and never "NFLX" — and matching only the ticker is why this panel read
    "no headlines mention your holdings" on a day three of them did.
    """
    patterns: dict[str, list[re.Pattern]] = {}
    for ticker in {t.upper() for t in tickers}:
        patterns[ticker] = [
            re.compile(rf"(?<![A-Za-z0-9]){re.escape(ticker)}(?![A-Za-z0-9])")
        ]
    for ticker, name in (names or {}).items():
        alias = company_alias(name)
        if alias:
            patterns.setdefault(ticker.upper(), []).append(
                re.compile(rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])",
                           re.IGNORECASE)
            )
    matched: list[dict] = []
    for item in items:
        text = f"{item.get('title', '')} {item.get('summary', '')}"
        for ticker, group in patterns.items():
            if any(pattern.search(text) for pattern in group):
                matched.append({**item, "matched_ticker": ticker})
                break
    return matched


async def fetch_news(tickers: list[str] | None = None,
                     names: dict[str, str] | None = None) -> dict:
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

    portfolio_news = match_portfolio_news(all_items, tickers or [], names)

    return {
        "portfolio_news": portfolio_news[:10],
        "market_news": all_items[:20],
        "fetched_at": _cache["fetched_at"],
    }

"""News as a running feed rather than a snapshot.

Headlines used to live only in a five-minute in-memory cache: a story vanished
the moment the RSS feed stopped carrying it, and a restart lost everything. The
store is deliberately *unscoped* — a headline is the same headline for every
customer, exactly like price_history — and which items are "yours" is decided
at read time by matching against your holdings.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from backend import db, news


@pytest.fixture
def store(tmp_path):
    db.set_db_path(tmp_path / "news.db")
    db.init_db()
    news._cache["items"] = []
    news._cache["fetched_at"] = 0.0
    yield db
    news._cache["items"] = []
    news._cache["fetched_at"] = 0.0


def item(n: int, days_old: float = 0, title: str = "", link: str = "") -> dict:
    when = datetime.now(UTC) - timedelta(days=days_old)
    return {
        "link": link or f"https://example.test/{n}",
        "title": title or f"Story {n}",
        "summary": "summary",
        "source": "CNBC",
        "published": when.isoformat(),
    }


def offline(monkeypatch, produce=None):
    """No network. Paging especially must never re-poll."""
    calls = []

    async def fake_poll():
        calls.append(1)
        return list(produce or [])

    monkeypatch.setattr(news, "_poll_feeds", fake_poll)
    return calls


def fetch(**kwargs):
    return asyncio.run(news.fetch_news(**kwargs))


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def test_headlines_are_stored_and_deduplicated(store):
    first = db.store_news_items([item(1), item(2)])
    again = db.store_news_items([item(1), item(2), item(3)])

    assert (first, again) == (2, 1)
    assert len(db.list_news_items()) == 3


def test_items_without_a_link_are_skipped(store):
    assert db.store_news_items([{"title": "no link"}]) == 0


def test_the_feed_is_newest_first(store):
    db.store_news_items([item(1, days_old=5), item(2, days_old=1), item(3, days_old=3)])

    assert [i["title"] for i in db.list_news_items()] == ["Story 2", "Story 3", "Story 1"]


def test_re_seeing_an_item_does_not_move_it_to_the_top(store):
    """Feeds re-publish. An item should not float forever on republication."""
    db.store_news_items([item(1, days_old=4)])
    db.store_news_items([item(2, days_old=0), item(1, days_old=4)])

    assert [i["title"] for i in db.list_news_items()] == ["Story 2", "Story 1"]


# ---------------------------------------------------------------------------
# The feed has a past
# ---------------------------------------------------------------------------


def test_the_feed_survives_an_empty_poll(store, monkeypatch):
    """The whole point: a story that has dropped out of the RSS feed is still
    in the feed the user reads."""
    db.store_news_items([item(1, days_old=2), item(2, days_old=3)])
    offline(monkeypatch, produce=[])

    result = fetch(tickers=[], names={})

    assert [i["title"] for i in result["market_news"]] == ["Story 1", "Story 2"]


def test_a_poll_adds_to_the_feed_rather_than_replacing_it(store, monkeypatch):
    db.store_news_items([item(1, days_old=3)])
    offline(monkeypatch, produce=[item(2, days_old=0)])

    result = fetch(tickers=[], names={})

    assert [i["title"] for i in result["market_news"]] == ["Story 2", "Story 1"]


def test_paging_goes_further_back_without_polling(store, monkeypatch):
    """Asking for older news is not a reason to re-poll the feeds."""
    db.store_news_items([item(n, days_old=n) for n in range(1, 6)])
    calls = offline(monkeypatch, produce=[])
    first = fetch(tickers=[], names={}, limit=2)
    calls.clear()

    older = fetch(tickers=[], names={}, before=first["next_before"], limit=2)

    assert calls == []
    assert [i["title"] for i in older["market_news"]] == ["Story 3", "Story 4"]


def test_the_cursor_is_the_oldest_item_in_the_slice(store, monkeypatch):
    db.store_news_items([item(n, days_old=n) for n in range(1, 4)])
    offline(monkeypatch, produce=[])

    result = fetch(tickers=[], names={}, limit=2)

    assert result["next_before"] == result["market_news"][-1]["published"]


def test_an_exhausted_feed_reports_no_cursor(store, monkeypatch):
    offline(monkeypatch, produce=[])

    result = fetch(tickers=[], names={})

    assert result["market_news"] == []
    assert result["next_before"] == ""


# ---------------------------------------------------------------------------
# Matching stays a read-time decision
# ---------------------------------------------------------------------------


def test_holdings_are_matched_across_the_whole_feed_not_just_todays(store, monkeypatch):
    db.store_news_items([
        item(1, days_old=9, title="Apple unveils something", link="https://example.test/old-aapl"),
        item(2, days_old=0, title="Unrelated market news", link="https://example.test/new"),
    ])
    offline(monkeypatch, produce=[])

    result = fetch(tickers=["AAPL"], names={"AAPL": "Apple"})

    assert [i["title"] for i in result["portfolio_news"]] == ["Apple unveils something"]
    assert result["portfolio_news"][0]["matched_ticker"] == "AAPL"


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def test_items_past_the_window_drop_out_of_the_feed(store, monkeypatch):
    db.store_news_items([item(1, days_old=45), item(2, days_old=1)])
    offline(monkeypatch, produce=[])

    assert [i["title"] for i in fetch(tickers=[], names={})["market_news"]] == ["Story 2"]


def test_the_sweep_deletes_what_the_read_already_hid(store):
    db.store_news_items([item(1, days_old=45), item(2, days_old=1)])

    assert db.purge_expired_news() == 1
    assert len(db.list_news_items()) == 1


def test_news_is_not_a_scoped_table(store):
    """One copy of a headline serves everyone; scoping would store it per
    account and re-fetch per account."""
    assert "news_items" not in db.SCOPED_TABLES


def test_a_full_slice_offers_more_and_a_short_one_does_not(store, monkeypatch):
    """The button appeared on a feed already showing everything it had, and
    clicking it did nothing. A full slice is the only evidence of more."""
    db.store_news_items([item(n, days_old=n) for n in range(1, 6)])
    offline(monkeypatch, produce=[])

    assert fetch(tickers=[], names={}, limit=2)["has_more"] is True
    assert fetch(tickers=[], names={}, limit=50)["has_more"] is False


def test_an_empty_feed_offers_nothing_more(store, monkeypatch):
    offline(monkeypatch, produce=[])

    assert fetch(tickers=[], names={})["has_more"] is False


def test_an_unusable_table_costs_the_history_not_the_feed(store, monkeypatch):
    """Postgres takes its schema from an owner-run migration, and RLS enabled
    with no policy denies everything — both are real states. News must fall
    back to the live poll rather than failing."""
    with db.connect() as conn:
        conn.execute("DROP TABLE news_items")
    offline(monkeypatch, produce=[item(1), item(2)])

    result = fetch(tickers=[], names={})

    assert [i["title"] for i in result["market_news"]] == ["Story 1", "Story 2"]
    assert db.store_news_items([item(3)]) == 0
    assert db.purge_expired_news() == 0

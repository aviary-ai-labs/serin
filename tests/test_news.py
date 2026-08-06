from __future__ import annotations

from backend.news import match_portfolio_news


def _item(title: str, summary: str = "") -> dict:
    return {"title": title, "summary": summary, "source": "Test", "link": "", "published": ""}


def test_matches_ticker_as_standalone_word():
    items = [_item("Waste Management (WM) Gains As Market Dips")]
    matched = match_portfolio_news(items, ["WM"])
    assert len(matched) == 1
    assert matched[0]["matched_ticker"] == "WM"


def test_does_not_match_english_word_casing():
    items = [_item("Oracle's stock has surged on AI hype. Now it has to deliver.")]
    assert match_portfolio_news(items, ["NOW"]) == []


def test_does_not_match_ticker_inside_other_word():
    items = [_item("Robinhood among brokers offering SpaceX shares")]
    assert match_portfolio_news(items, ["HOOD"]) == []


def test_matches_in_summary():
    items = [_item("Chipmakers rally", "INTC led the gains after upbeat guidance.")]
    matched = match_portfolio_news(items, ["INTC"])
    assert len(matched) == 1

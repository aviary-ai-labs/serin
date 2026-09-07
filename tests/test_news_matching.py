"""Matching headlines to holdings.

The panel read "No headlines mention your holdings right now" while three of
the twenty fetched stories were about holdings. It matched tickers only, and
a newsroom writes "Netflix" — never "NFLX".

The opposite error is worse, so the tests below spend more effort on what must
*not* match: a story about 401(k) millionaires is not news about a Fidelity
fund, and telling someone it is trains them to ignore the panel.
"""

from __future__ import annotations

import pytest
from backend.news import company_alias, match_portfolio_news


def story(title, summary=""):
    return {"title": title, "summary": summary, "source": "CNBC"}


def matched(items, tickers, names=None):
    return [item["matched_ticker"] for item in
            match_portfolio_news(items, tickers, names)]


# --- the bug: names, not tickers -------------------------------------------


def test_a_headline_naming_the_company_matches():
    items = [story("Netflix stock climbs after subscriber beat")]
    assert matched(items, ["NFLX"], {"NFLX": "Netflix Inc."}) == ["NFLX"]


def test_a_ticker_still_matches_on_its_own():
    items = [story("NFLX upgraded at Morgan Stanley")]
    assert matched(items, ["NFLX"], {}) == ["NFLX"]


def test_corporate_furniture_is_stripped_before_matching():
    """"Alphabet Inc. Class C Common Stock" is never how a story says it."""
    items = [story("Alphabet reveals new Gemini pricing")]
    assert matched(items, ["GOOG"],
                   {"GOOG": "Alphabet Inc. Class C Common Stock"}) == ["GOOG"]


@pytest.mark.parametrize("name,alias", [
    ("Apple Inc.", "Apple"),
    ("Tesla, Inc.", "Tesla"),
    ("Alibaba Group Holding Ltd. ADR", "Alibaba"),
    ("Meta Platforms Inc. Class A Common Stock", "Meta Platforms"),
    ("Robinhood Markets Inc. Class A Common Stock", "Robinhood Markets"),
    ("Nokia Corporation Sponsored ADR", "Nokia"),
    ("Pagaya Technologies Ltd. Class A Common Stock", "Pagaya"),
])
def test_the_alias_is_what_a_journalist_would_write(name, alias):
    assert company_alias(name) == alias


# --- what must not match ---------------------------------------------------


def test_an_issuers_own_name_is_not_a_holding():
    """"There's a new record number of 401(k) millionaires" matched two
    Fidelity funds. The article is about retirement saving, and Fidelity is
    the administrator, not the subject."""
    assert company_alias("Fidelity Balanced Fund") is None
    items = [story("There's a new record number of 401(k) millionaires",
                   "Fidelity said balances rose again this quarter.")]
    assert matched(items, ["FBALX"], {"FBALX": "Fidelity Balanced Fund"}) == []


def test_a_fund_is_not_matched_by_name():
    """"One state dominates this list of best places to retire" matched SPY
    through "State Street". A fund's name belongs to its issuer, not to it."""
    for name in ("State Street SPDR S&P 500 ETF Trust",
                 "Fidelity Government Money Market Fund",
                 "Vanguard Total Stock Market Index Fund"):
        assert company_alias(name) is None, name


def test_a_ticker_that_is_an_english_word_stays_case_sensitive():
    """NOW is ServiceNow. "Now" is a word that starts sentences."""
    items = [story("Now is the time to rebalance, strategists say")]
    assert matched(items, ["NOW"], {}) == []


def test_a_company_name_inside_a_longer_word_does_not_match():
    items = [story("Applebee's parent reports a strong quarter")]
    assert matched(items, ["AAPL"], {"AAPL": "Apple Inc."}) == []


def test_a_dot_inside_a_name_survives_but_a_trailing_one_does_not():
    """The dot in "JD.com" spells the name; the one after "Inc" separates."""
    assert company_alias("JD.com, Inc. Sponsored ADR") == "JD.com"


def test_a_name_too_short_to_be_distinctive_is_not_used():
    assert company_alias("Inc.") is None
    assert company_alias("The Co.") is None


def test_a_fund_word_inside_a_company_name_does_not_disqualify_it():
    """"etf" sits inside "Netflix". A substring check dropped the one holding
    the feeds mention most."""
    assert company_alias("Netflix Inc.") == "Netflix"
    assert company_alias("Trust Bank Corp") is None    # a real fund word


def test_an_unrelated_headline_matches_nothing():
    items = [story("U.S. sanctions Turkish bank accused of enabling Iran")]
    assert matched(items, ["AAPL", "TSLA"],
                   {"AAPL": "Apple Inc.", "TSLA": "Tesla, Inc."}) == []


def test_one_headline_is_credited_to_a_single_holding():
    """Two holdings in one story is one card, not two."""
    items = [story("Apple and Tesla both fell on the open")]
    assert len(matched(items, ["AAPL", "TSLA"],
                       {"AAPL": "Apple Inc.", "TSLA": "Tesla, Inc."})) == 1

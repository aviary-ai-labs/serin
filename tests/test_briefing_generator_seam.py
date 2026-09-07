"""The briefing generator seam — core's side.

Briefings are free forever (``docs/BUSINESS-MODEL.md``), so this is a
*pattern 2* seam: core keeps a complete implementation and a pack may supply a
richer one on top. The property that matters is the failure posture. A briefing
is a scheduled job nobody is watching at 07:30, so an installed generator that
breaks must cost the better briefing, never the briefing.
"""

from __future__ import annotations

import asyncio

import pytest
from backend import briefings, db
from backend.models import PositionIn


@pytest.fixture
def book(tmp_path, monkeypatch):
    db.set_db_path(tmp_path / "briefing-seam.db")
    db.init_db()
    db.create_position(PositionIn(
        symbol="AAPL", name="Apple", broker="robinhood", asset_type="stock",
        quantity=10, average_cost=100.0, current_price=150.0,
    ))

    async def no_news(tickers=None, names=None):
        return {"items": []}

    monkeypatch.setattr(briefings, "fetch_news", no_news)
    return db


@pytest.fixture(autouse=True)
def clear_seam():
    yield
    briefings.set_generator(None)


def run(briefing_id):
    asyncio.run(briefings.run_daily_briefing(briefing_id))


def new_briefing():
    return db.create_briefing({}, model="test-model", trigger="manual").id


def stub_core_model(monkeypatch, text="# Daily Briefing\n\n## Summary\nCore briefing."):
    async def fake_call_model(prompt):
        return text, {"input_tokens": 100, "output_tokens": 50, "model": "claude-haiku"}

    monkeypatch.setattr(briefings, "call_model", fake_call_model)


# ---------------------------------------------------------------------------


def test_no_generator_installed_uses_cores_own_briefing(book, monkeypatch):
    stub_core_model(monkeypatch)
    assert briefings.generator_installed() is False

    briefing_id = new_briefing()
    run(briefing_id)

    stored = db.get_briefing(briefing_id)
    assert stored.status == "done"
    assert "Core briefing." in stored.output_markdown


def test_an_installed_generator_replaces_the_prose(book, monkeypatch):
    stub_core_model(monkeypatch)

    async def premium(context):
        return "# Premium\n\n## Summary\nPremium briefing.", {
            "input_tokens": 900, "output_tokens": 400, "model": "claude-sonnet-5",
        }

    briefings.set_generator(premium)

    briefing_id = new_briefing()
    run(briefing_id)

    stored = db.get_briefing(briefing_id)
    assert stored.status == "done"
    assert "Premium briefing." in stored.output_markdown
    assert stored.summary == "Premium briefing."
    assert stored.model_cost_usd > 0


def test_the_generator_is_handed_what_core_already_computed(book, monkeypatch):
    """It must not have to refetch the snapshot or the news."""
    stub_core_model(monkeypatch)
    seen = {}

    async def premium(context):
        seen.update(context)
        return "# P\n\n## Summary\nok", {"input_tokens": 1, "output_tokens": 1, "model": "x"}

    briefings.set_generator(premium)
    run(new_briefing())

    assert set(seen) == {"snapshot", "news", "style"}
    assert seen["style"] == "operator"
    assert seen["snapshot"]["positions"][0]["symbol"] == "AAPL"
    assert "items" in seen["news"]


def test_the_style_reaches_the_generator(book, monkeypatch):
    stub_core_model(monkeypatch)
    seen = {}

    async def premium(context):
        seen.update(context)
        return "# P\n\n## Summary\nok", {"input_tokens": 1, "output_tokens": 1, "model": "x"}

    briefings.set_generator(premium)
    asyncio.run(briefings.run_daily_briefing(new_briefing(), style="analyst"))

    assert seen["style"] == "analyst"


def test_a_broken_generator_falls_back_to_core_not_to_an_error(book, monkeypatch):
    """The whole point of pattern 2: degrade, don't disappear."""
    stub_core_model(monkeypatch)

    async def broken(context):
        raise RuntimeError("pack exploded")

    briefings.set_generator(broken)

    briefing_id = new_briefing()
    run(briefing_id)

    stored = db.get_briefing(briefing_id)
    assert stored.status == "done"
    assert "Core briefing." in stored.output_markdown
    assert not stored.error


def test_a_provider_failure_still_reports_an_error(book, monkeypatch):
    """Fallback covers a broken *pack*, not a broken provider — otherwise a
    dead API key would look like a successful briefing."""
    async def dead(prompt):
        raise RuntimeError("authentication_error")

    monkeypatch.setattr(briefings, "call_model", dead)

    briefing_id = new_briefing()
    run(briefing_id)

    stored = db.get_briefing(briefing_id)
    assert stored.status == "error"
    assert stored.error


def test_the_seam_can_be_cleared(book, monkeypatch):
    stub_core_model(monkeypatch)

    async def premium(context):
        return "# P\n\n## Summary\nPremium.", {"input_tokens": 1, "output_tokens": 1, "model": "x"}

    briefings.set_generator(premium)
    assert briefings.generator_installed() is True

    briefings.set_generator(None)

    briefing_id = new_briefing()
    run(briefing_id)
    assert "Core briefing." in db.get_briefing(briefing_id).output_markdown


def test_core_ships_no_premium_briefing_implementation():
    """Pattern 2 cuts both ways: the enhancement stays out of tree."""
    import pathlib

    backend = pathlib.Path(__file__).resolve().parents[1] / "backend"
    assert [p.name for p in backend.glob("*premium*")] == []


def test_a_generator_may_decline_and_core_takes_over(book, monkeypatch, caplog):
    """Returning None is how a lapsed licence steps aside. It is the ordinary
    path, not a fault, so it must not be logged as one."""
    stub_core_model(monkeypatch)

    async def declines(context):
        return None

    briefings.set_generator(declines)

    briefing_id = new_briefing()
    with caplog.at_level("WARNING", logger="backend.briefings"):
        run(briefing_id)

    stored = db.get_briefing(briefing_id)
    assert stored.status == "done"
    assert "Core briefing." in stored.output_markdown
    assert [r for r in caplog.records if "falling back" in r.message] == []


def test_a_raising_generator_is_logged_unlike_a_declining_one(book, monkeypatch, caplog):
    stub_core_model(monkeypatch)

    async def broken(context):
        raise RuntimeError("pack exploded")

    briefings.set_generator(broken)
    with caplog.at_level("WARNING", logger="backend.briefings"):
        run(new_briefing())

    assert any("falling back" in r.message for r in caplog.records)

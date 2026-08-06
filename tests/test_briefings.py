from __future__ import annotations

from datetime import UTC

from backend import db
from backend.briefings import build_briefing_prompt, build_portfolio_snapshot, friendly_model_error
from backend.config import settings
from backend.main import app
from backend.models import PositionIn
from fastapi.testclient import TestClient


def test_briefing_run_returns_503_without_api_key(tmp_path, monkeypatch):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()
    monkeypatch.setattr(settings, "ai_provider", "anthropic_api")
    monkeypatch.setattr(settings, "anthropic_api_key", "")

    client = TestClient(app)
    response = client.post("/api/briefings/run")

    assert response.status_code == 503
    assert "ANTHROPIC_API_KEY" in response.text


def test_briefing_prompt_contains_snapshot_and_news(tmp_path):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()
    db.create_position(
        PositionIn(
            symbol="TSLA",
            name="Tesla",
            broker="manual",
            asset_type="stock",
            quantity=2,
            average_cost=200,
            current_price=220,
            sector="Consumer Cyclical",
        )
    )
    snapshot = build_portfolio_snapshot()
    news = {
        "portfolio_news": [{"title": "TSLA shares move", "source": "Example"}],
        "market_news": [{"title": "Market opens mixed", "source": "Example"}],
    }

    prompt = build_briefing_prompt(snapshot, news)

    assert "Portfolio snapshot JSON" in prompt
    assert "Market/news JSON" in prompt
    assert "TSLA" in prompt
    assert "TSLA shares move" in prompt
    assert "Do not give personalized investment advice" in prompt


def test_briefing_prompt_styles_have_distinct_shapes(tmp_path):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()
    snapshot = {"positions": [], "total_value": 0}
    news = {"portfolio_news": [], "market_news": []}

    operator = build_briefing_prompt(snapshot, news, style="operator")
    analyst = build_briefing_prompt(snapshot, news, style="analyst")
    executive = build_briefing_prompt(snapshot, news, style="executive")

    assert "Briefing style: Operator Brief" in operator
    assert "## Risk Flags" in operator
    assert "Briefing style: Analyst Brief" in analyst
    assert "## Exposure Themes" in analyst
    assert "Briefing style: Executive Brief" in executive
    assert "## Top Signals" in executive
    assert "Keep it under 300 words" in executive


def test_delete_briefing(tmp_path):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()
    briefing = db.create_briefing(snapshot={}, model="test-model")
    db.finish_briefing(briefing.id, status="error", error="boom")

    client = TestClient(app)
    response = client.delete(f"/api/briefings/{briefing.id}")
    assert response.status_code == 200
    assert db.get_briefing(briefing.id) is None

    missing = client.delete(f"/api/briefings/{briefing.id}")
    assert missing.status_code == 404


def test_friendly_model_error_hides_raw_auth_json(monkeypatch):
    monkeypatch.setattr(settings, "ai_provider", "claude_cli")

    error = friendly_model_error(
        RuntimeError(
            'Failed to authenticate. API Error: 401 {"type":"error","error":{"type":"authentication_error"}}'
        )
    )

    assert "Claude CLI authentication failed" in error
    assert "claude auth login --claudeai" in error
    assert '{"type"' not in error


def test_snapshot_includes_recent_transactions(tmp_path):
    from datetime import datetime, timedelta

    from backend.models import TransactionIn

    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()
    yesterday = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    long_ago = (datetime.now(UTC) - timedelta(days=40)).date().isoformat()
    db.create_transaction(TransactionIn(symbol="MSFT", action="dividend", price=12.5, occurred_at=yesterday))
    db.create_transaction(TransactionIn(symbol="OLD", action="buy", quantity=1, price=10, occurred_at=long_ago))

    snapshot = build_portfolio_snapshot()

    recent = snapshot["recent_transactions"]
    assert len(recent) == 1
    assert recent[0]["symbol"] == "MSFT"
    assert recent[0]["action"] == "dividend"


def test_briefing_prompt_mentions_recent_activity(tmp_path):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()
    snapshot = build_portfolio_snapshot()
    prompt = build_briefing_prompt(snapshot, {"portfolio_news": [], "market_news": []})
    assert "recent_transactions" in prompt


def test_briefing_estimate_endpoint(tmp_path, monkeypatch):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()
    monkeypatch.setattr(settings, "ai_provider", "deepseek")
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")

    client = TestClient(app)
    payload = client.get("/api/briefings/estimate").json()

    assert payload["provider"] == "deepseek"
    assert payload["model"] == settings.deepseek_model
    assert payload["estimated_cost_usd"] > 0
    assert "basis" in payload

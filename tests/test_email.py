from __future__ import annotations

import pytest
from backend import db, emailer
from backend.config import settings
from backend.emailer import briefing_subject, markdown_to_html, render_briefing_email_html
from backend.main import app
from backend.models import Briefing
from fastapi.testclient import TestClient


def _briefing(**overrides) -> Briefing:
    base = dict(
        id=1,
        status="done",
        summary="All good",
        output_markdown="# Daily Briefing - 2026-06-10\n## Summary\n- **HOOD** down -27%\n---\n*Note: estimates.*",
        model="deepseek-v4-flash",
        created_at="2026-06-10T14:30:00+00:00",
        completed_at="2026-06-10T14:31:00+00:00",
    )
    base.update(overrides)
    return Briefing(**base)


@pytest.fixture
def email_env(monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_username", "serin@example.com")
    monkeypatch.setattr(settings, "smtp_password", "secret")
    monkeypatch.setattr(settings, "email_from", "")
    monkeypatch.setattr(settings, "email_to", "ethan@example.com")
    return monkeypatch


def test_markdown_to_html_renders_structure():
    html_out = markdown_to_html("# Title\n## Section\n- **bold** item\n1. first\n---\n*note*")
    assert "<h1" in html_out and "Title" in html_out
    assert "<h2" in html_out and "Section" in html_out
    assert "<strong>bold</strong>" in html_out
    assert "<ol" in html_out and "first" in html_out
    assert "<hr" in html_out
    assert "<em>note</em>" in html_out


def test_markdown_to_html_escapes_html():
    html_out = markdown_to_html("hello <script>alert(1)</script>")
    assert "<script>" not in html_out
    assert "&lt;script&gt;" in html_out


def test_subject_uses_briefing_date():
    assert briefing_subject(_briefing()) == "Serin Daily Briefing — Wed, Jun 10 2026"


def test_email_html_includes_disclaimer():
    html_out = render_briefing_email_html(_briefing())
    assert "not investment advice" in html_out


def test_send_requires_configuration(monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "")
    with pytest.raises(RuntimeError, match="not configured"):
        emailer.send_briefing_email(_briefing())


def test_send_rejects_unfinished_briefing(email_env):
    with pytest.raises(RuntimeError, match="completed"):
        emailer.send_briefing_email(_briefing(status="error", output_markdown=""))


def test_email_endpoint_503_when_unconfigured(tmp_path, monkeypatch):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()
    monkeypatch.setattr(settings, "smtp_host", "")
    client = TestClient(app)
    assert client.post("/api/briefings/1/email").status_code == 503


def test_email_endpoint_sends_and_marks(tmp_path, email_env, monkeypatch):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()
    briefing = db.create_briefing(snapshot={}, model="m")
    db.finish_briefing(briefing.id, status="done", output_markdown="# B", summary="s")

    sent = {}

    def fake_send(item):
        sent["id"] = item.id
        return settings.email_to

    monkeypatch.setattr(emailer, "send_briefing_email", fake_send)
    client = TestClient(app)
    response = client.post(f"/api/briefings/{briefing.id}/email")

    assert response.status_code == 200
    assert response.json()["to"] == "ethan@example.com"
    assert sent["id"] == briefing.id
    assert db.get_briefing(briefing.id).emailed_at


def test_email_endpoint_rejects_running_briefing(tmp_path, email_env):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()
    briefing = db.create_briefing(snapshot={}, model="m")
    client = TestClient(app)
    assert client.post(f"/api/briefings/{briefing.id}/email").status_code == 400


def test_schedule_accepts_email_enabled(tmp_path):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()
    client = TestClient(app)
    saved = client.put(
        "/api/schedule",
        json={"enabled": True, "time": "07:00", "timezone": "UTC", "email_enabled": True},
    ).json()
    assert saved["email_enabled"] is True
    assert client.get("/api/schedule").json()["email_enabled"] is True

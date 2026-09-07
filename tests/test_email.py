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


# --- the alt-sender seam -----------------------------------------------

@pytest.fixture(autouse=True)
def _clean_alt_sender():
    """The seam is a module-level global — leave it as core found it, or
    one test's pack simulation leaks into every test after it."""
    yield
    emailer.set_alt_sender(None)


def test_email_ready_reflects_smtp_when_nothing_else_is_installed(email_env):
    assert emailer.email_ready() is True
    email_env.setattr(settings, "smtp_host", "")
    assert emailer.email_ready() is False


def test_an_installed_alt_sender_overrides_smtp_readiness(monkeypatch):
    """A pack's relay can be ready when self-host SMTP isn't (the Cloud/
    Intelligence case) — and, just as important, be reported NOT ready even
    though it's installed (an expired license), so the UI never offers a
    toggle that will just fail every run."""
    monkeypatch.setattr(settings, "smtp_host", "")
    ready = {"value": True}
    emailer.set_alt_sender(lambda briefing: "someone@example.com", lambda: ready["value"])
    assert emailer.email_ready() is True
    ready["value"] = False
    assert emailer.email_ready() is False
    assert emailer.alt_sender_installed() is True


def test_deliver_prefers_the_alt_sender_when_installed(monkeypatch):
    calls = []
    emailer.set_alt_sender(lambda briefing: calls.append(briefing.id) or "relay@example.com")
    assert emailer.deliver_scheduled_briefing_email(_briefing()) == "relay@example.com"
    assert calls == [1]


def test_deliver_falls_back_to_smtp_when_nothing_is_installed(email_env, monkeypatch):
    sent = {}

    def fake_send(b):
        sent["id"] = b.id
        return "smtp@example.com"

    monkeypatch.setattr(emailer, "send_briefing_email", fake_send)
    assert emailer.deliver_scheduled_briefing_email(_briefing()) == "smtp@example.com"
    assert sent["id"] == 1


def test_alt_recipient_is_display_only_and_swallows_errors():
    assert emailer.alt_recipient() == ""  # nothing installed
    emailer.set_alt_sender(lambda b: "x", None, lambda: "shown@example.com")
    assert emailer.alt_recipient() == "shown@example.com"
    emailer.set_alt_sender(lambda b: "x", None, lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert emailer.alt_recipient() == ""


def test_config_email_fields_use_the_alt_path_when_smtp_is_unset(tmp_path, monkeypatch):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()
    monkeypatch.setattr(settings, "smtp_host", "")
    emailer.set_alt_sender(lambda b: "x", lambda: True, lambda: "relay@example.com")
    payload = TestClient(app).get("/api/config").json()
    assert payload["email_configured"] is True
    assert payload["email_to"] == "relay@example.com"


def test_email_endpoint_503_message_names_the_plan_not_env_when_alt_is_installed(tmp_path):
    """An installed-but-not-ready alt sender means this account belongs to a
    paid plan whose delivery is temporarily down — telling them to edit a
    .env file they don't have would be actively wrong."""
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()
    emailer.set_alt_sender(lambda b: "x", lambda: False)
    resp = TestClient(app).post("/api/briefings/1/email")
    assert resp.status_code == 503
    assert ".env" not in resp.json()["detail"]


def test_email_endpoint_uses_deliver_so_alt_sender_is_reached(tmp_path):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()
    briefing = db.create_briefing(snapshot={}, model="m")
    db.finish_briefing(briefing.id, status="done", output_markdown="# B", summary="s")

    reached = []
    emailer.set_alt_sender(lambda b: reached.append(b.id) or "relay@example.com", lambda: True)
    resp = TestClient(app).post(f"/api/briefings/{briefing.id}/email")

    assert resp.status_code == 200
    assert resp.json()["to"] == "relay@example.com"
    assert reached == [briefing.id]

"""Expo push: registration, validation, send + token pruning."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from backend import db, push
from backend.main import app
from fastapi.testclient import TestClient


@pytest.fixture
def fresh(tmp_path):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()


def test_register_endpoint_validates_and_dedupes(fresh):
    client = TestClient(app)

    bad = client.post("/api/v1/push/register", json={"token": "not-a-token"})
    assert bad.status_code == 400

    ok = client.post("/api/v1/push/register", json={"token": "ExponentPushToken[abc123]"})
    assert ok.status_code == 200
    assert ok.json()["devices"] == 1

    again = client.post("/api/v1/push/register", json={"token": "ExponentPushToken[abc123]"})
    assert again.json()["devices"] == 1  # deduped


def test_send_pushes_and_prunes_dead_tokens(fresh, monkeypatch):
    push.register_token("ExponentPushToken[alive]")
    push.register_token("ExponentPushToken[dead]")

    sent_payloads = {}

    def fake_post(url, json=None, timeout=None):
        sent_payloads["url"] = url
        sent_payloads["messages"] = json
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"data": [
                {"status": "ok"},
                {"status": "error", "message": "gone", "details": {"error": "DeviceNotRegistered"}},
            ]},
        )

    monkeypatch.setattr("httpx.post", fake_post)

    attempted = push.send_briefing_ready("Markets moved.")

    assert attempted == 2
    assert sent_payloads["url"] == push.EXPO_PUSH_URL
    assert sent_payloads["messages"][0]["to"] == "ExponentPushToken[alive]"
    # Dead token pruned; live one kept.
    assert push.list_tokens() == ["ExponentPushToken[alive]"]


def test_send_with_no_tokens_is_a_noop(fresh, monkeypatch):
    def must_not_post(*args, **kwargs):
        raise AssertionError("no HTTP when no tokens registered")

    monkeypatch.setattr("httpx.post", must_not_post)
    assert push.send_briefing_ready() == 0


def test_pairing_endpoint_is_gone(fresh):
    """QR pairing existed so the phone could point at a self-hosted box. The
    app is Cloud-only now, so the endpoint has no caller — and it handed out a
    session token to anyone who could reach it, which is a thing worth being
    sure has actually gone rather than merely stopped being linked."""
    client = TestClient(app)
    body = client.get("/api/pairing").text
    assert '"serin"' not in body and '"token"' not in body

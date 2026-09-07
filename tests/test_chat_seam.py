"""The seam the chat renderer talks to, from core's side.

Core ships the renderer and nothing else: no prompt, no model call, no
entitlement logic. What core has to guarantee is the contract the renderer
depends on — that a missing pack answers 404 (so the tab, and every trace of
chat, disappears), and that a present one has its payload passed through
untouched, including the stream URL the renderer is told to read.
"""

from __future__ import annotations

import pytest
from backend import db
from backend.connectors import registry

# Aliased: pytest tries to collect anything named Test* as a test class.
from backend.connectors.base import ConnectorManifest, InsightConnector
from backend.connectors.base import TestResult as ConnectorTestResult
from backend.main import app
from fastapi.testclient import TestClient

STREAM_URL = "/api/pack/chat/stream"


def _chat_connector(entitled: bool):
    class ChatConnector(InsightConnector):
        manifest = ConnectorManifest(
            id="chat",
            name="Chat",
            kind="insight",
            description="Conversational access to the portfolio.",
            default_enabled=True,
            connect_method="none",
        )

        def run(self, context: dict | None = None) -> dict:
            if not entitled:
                return {
                    "entitled": False,
                    "feature": "chat",
                    "message": "Chat is a Serin Intelligence feature.",
                }
            return {
                "entitled": True,
                "feature": "chat",
                "stream_url": STREAM_URL,
                "disclaimer": "Context, never trade directives.",
            }

        def test(self) -> ConnectorTestResult:
            return ConnectorTestResult(ok=entitled, message="")

    return ChatConnector


@pytest.fixture
def client(tmp_path):
    db.set_db_path(tmp_path / "chat-seam.db")
    db.init_db()
    return TestClient(app)


@pytest.fixture(autouse=True)
def clean_registry():
    yield
    registry._REGISTRY.pop("chat", None)


def test_no_pack_means_404_and_therefore_no_chat_tab(client):
    """The renderer keys 'absent' off exactly this status."""
    assert client.post("/api/connectors/chat/run", json={}).status_code == 404


def test_an_unlicensed_pack_returns_its_own_upsell(client):
    registry.register(_chat_connector(entitled=False))

    body = client.post("/api/connectors/chat/run", json={}).json()

    assert body["entitled"] is False
    assert "Intelligence" in body["message"]
    assert "stream_url" not in body


def test_a_licensed_pack_hands_the_renderer_a_stream_url(client):
    """Core hardcodes nothing about chat's wire protocol — the pack names it."""
    registry.register(_chat_connector(entitled=True))

    body = client.post("/api/connectors/chat/run", json={}).json()

    assert body["entitled"] is True
    assert body["stream_url"] == STREAM_URL
    assert body["disclaimer"]


def test_core_ships_no_chat_implementation():
    """The pledge cuts both ways: chat is a paid feature, so the open-source
    repo must not accidentally grow one."""
    import pathlib

    backend = pathlib.Path(__file__).resolve().parents[1] / "backend"
    offenders = [p.name for p in backend.glob("*.py") if "chat" in p.name.lower()]

    assert offenders == []

"""The HTTP agent surface — /api/agent, and the token endpoints beside it.

The tool endpoints are the easy half. The half worth regression-testing is the
boundary: an agent token must reach the agent surface and nothing else, and
above all it must not be able to mint or list its own successors. That last one
is enforced by a path-prefix test in the middleware, which is exactly the kind
of check that a later refactor loosens by accident.
"""

from __future__ import annotations

import pytest
from backend import agent_tokens, auth, db
from backend.config import settings
from backend.main import app
from backend.models import PositionIn
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path):
    db.set_db_path(tmp_path / "agent-api.db")
    db.init_db()
    db.create_position(PositionIn(
        symbol="AAPL", name="Apple", broker="robinhood", asset_type="stock",
        quantity=10, average_cost=100.0, current_price=150.0, sector="Technology",
    ))
    return TestClient(app)


@pytest.fixture(autouse=True)
def unlocked(monkeypatch):
    """Most tests want the self-host default: no app lock, no pack authorizer."""
    monkeypatch.setattr(settings, "auth_password", "")
    yield
    auth.set_authorizer(None)


@pytest.fixture
def locked(monkeypatch):
    monkeypatch.setattr(settings, "auth_password", "hunter2")
    return auth.session_token()


# ---------------------------------------------------------------------------
# Tool surface
# ---------------------------------------------------------------------------


def test_manifest_describes_the_surface(client):
    body = client.get("/api/agent").json()

    assert body["name"] == "serin"
    assert body["read_only"] is True
    assert body["tool_count"] == len(body["tools"])
    assert "get_portfolio_summary" in body["tools"]


def test_tools_endpoint_is_mcp_shaped(client):
    body = client.get("/api/agent/tools").json()

    assert body["tools"]
    for entry in body["tools"]:
        assert set(entry) == {"name", "description", "inputSchema"}


def test_calling_a_tool_returns_its_result(client):
    response = client.post("/api/agent/tools/get_portfolio_summary", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["tool"] == "get_portfolio_summary"
    assert body["result"]["total_value"] == pytest.approx(1500)


def test_calling_a_tool_with_arguments(client):
    body = client.post("/api/agent/tools/get_position", json={"symbol": "AAPL"}).json()

    assert body["result"]["symbol"] == "AAPL"
    assert body["result"]["quantity"] == pytest.approx(10)


def test_unknown_tool_is_404(client):
    response = client.post("/api/agent/tools/get_the_future", json={})

    assert response.status_code == 404


def test_bad_arguments_are_400_with_a_correctable_message(client):
    response = client.post("/api/agent/tools/get_position", json={"ticker": "AAPL"})

    assert response.status_code == 400
    assert "Unknown argument" in response.json()["detail"]


def test_missing_required_argument_is_400(client):
    response = client.post("/api/agent/tools/get_position", json={})

    assert response.status_code == 400
    assert "requires symbol" in response.json()["detail"]


def test_context_markdown_is_servable_text(client):
    response = client.get("/api/agent/context.md")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "# Portfolio snapshot" in response.text
    assert "AAPL" in response.text
    assert "not financial advice" in response.text


def test_context_markdown_survives_an_empty_portfolio(tmp_path):
    db.set_db_path(tmp_path / "empty.db")
    db.init_db()

    response = TestClient(app).get("/api/agent/context.md")

    assert response.status_code == 200
    assert "# Portfolio snapshot" in response.text


def test_the_agent_surface_is_published_in_openapi(client):
    paths = client.get("/openapi.json").json()["paths"]

    assert "/api/agent/tools/{tool_name}" in paths
    assert "/api/agent/context.md" in paths


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------


def test_creating_a_token_returns_the_plaintext_exactly_once(client):
    created = client.post("/api/settings/agent-tokens", json={"name": "Claude Desktop"}).json()

    assert created["token"].startswith(agent_tokens.TOKEN_PREFIX)
    listed = client.get("/api/settings/agent-tokens").json()["tokens"]
    assert listed[0]["name"] == "Claude Desktop"
    assert "token" not in listed[0]
    assert "token_sha256" not in listed[0]


def test_revoking_a_token(client):
    created = client.post("/api/settings/agent-tokens", json={"name": "laptop"}).json()

    assert client.delete(f"/api/settings/agent-tokens/{created['id']}").status_code == 200
    assert client.get("/api/settings/agent-tokens").json()["tokens"] == []


def test_revoking_an_unknown_token_is_404(client):
    assert client.delete("/api/settings/agent-tokens/nope").status_code == 404


def test_unknown_scope_is_rejected(client):
    response = client.post("/api/settings/agent-tokens", json={"name": "x", "scope": "write"})

    assert response.status_code == 400


def test_the_listing_says_whether_this_is_a_multiuser_deployment(client):
    assert client.get("/api/settings/agent-tokens").json()["multiuser"] is False


# ---------------------------------------------------------------------------
# The boundary — what an agent token may and may not reach
# ---------------------------------------------------------------------------


def test_locked_instance_refuses_an_anonymous_agent_request(client, locked):
    assert client.get("/api/agent/tools").status_code == 401


def test_agent_token_reaches_the_agent_surface(client, locked):
    _, token = agent_tokens.issue("desktop")
    headers = {"Authorization": f"Bearer {token}"}

    assert client.get("/api/agent", headers=headers).status_code == 200
    assert client.get("/api/agent/tools", headers=headers).status_code == 200
    assert client.get("/api/agent/context.md", headers=headers).status_code == 200
    assert client.post(
        "/api/agent/tools/get_portfolio_summary", json={}, headers=headers
    ).status_code == 200


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/positions"),
        ("get", "/api/portfolio"),
        ("get", "/api/backup"),
        ("get", "/api/briefings"),
        ("post", "/api/positions"),
    ],
)
def test_agent_token_is_refused_everywhere_else(client, locked, method, path):
    _, token = agent_tokens.issue("desktop")

    response = getattr(client, method)(path, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 403
    assert "scoped to read" in response.json()["detail"]


def test_agent_token_cannot_mint_successors(client, locked):
    """The whole point of scoping: a leaked token must not become permanent."""
    _, token = agent_tokens.issue("desktop")
    headers = {"Authorization": f"Bearer {token}"}

    assert client.get("/api/settings/agent-tokens", headers=headers).status_code == 403
    assert client.post(
        "/api/settings/agent-tokens", json={"name": "second"}, headers=headers
    ).status_code == 403


def test_a_revoked_token_stops_working(client, locked):
    record, token = agent_tokens.issue("desktop")
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/agent/tools", headers=headers).status_code == 200

    agent_tokens.revoke(record["id"])

    assert client.get("/api/agent/tools", headers=headers).status_code == 401


def test_revoking_an_agent_token_leaves_the_session_standing(client, locked):
    record, token = agent_tokens.issue("desktop")
    agent_tokens.revoke(record["id"])

    session = {"Authorization": f"Bearer {locked}"}

    assert client.get("/api/positions", headers=session).status_code == 200
    assert client.get("/api/agent/tools", headers=session).status_code == 200


def test_the_session_token_still_reaches_everything(client, locked):
    headers = {"Authorization": f"Bearer {locked}"}

    assert client.get("/api/positions", headers=headers).status_code == 200
    assert client.get("/api/settings/agent-tokens", headers=headers).status_code == 200


def test_a_made_up_agent_token_is_refused(client, locked):
    headers = {"Authorization": f"Bearer {agent_tokens.TOKEN_PREFIX}forged"}

    assert client.get("/api/agent/tools", headers=headers).status_code == 401


def test_an_unlocked_instance_needs_no_token_at_all(client):
    """Self-host default: the app lock is off, so the agent surface is open
    like every other endpoint. Tokens matter once the lock is on."""
    assert client.get("/api/agent/tools").status_code == 200

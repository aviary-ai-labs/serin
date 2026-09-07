"""Agent access on a shared deployment — the scope boundary.

This is the highest-risk thing in the agent surface. An agent client has no
session and no cookie, so the scope cannot come from the request context; it
has to come from the credential, before any query runs. Getting it wrong does
not throw — it quietly answers one customer with another customer's portfolio.

The provider installed here **raises unless a scope has been forced**, which is
how a real deployment behaves for a request with nobody signed in. So every
passing test below is evidence that the token, not the ambient context, is what
established the scope.
"""

from __future__ import annotations

import pytest
from backend import agent_tokens, auth, db, scope
from backend.config import settings
from backend.main import app
from backend.models import PositionIn
from fastapi.testclient import TestClient


@pytest.fixture
def shared(tmp_path, monkeypatch):
    """Two accounts on one deployment, each holding a different book."""
    db.set_db_path(tmp_path / "shared.db")
    db.init_db()
    monkeypatch.setattr(settings, "auth_password", "")

    with scope.using("user-a"):
        db.create_position(PositionIn(
            symbol="AAPL", name="Apple", broker="robinhood", asset_type="stock",
            quantity=10, average_cost=100.0, current_price=150.0,
        ))
    with scope.using("user-b"):
        db.create_position(PositionIn(
            symbol="TSLA", name="Tesla", broker="fidelity", asset_type="stock",
            quantity=1, average_cost=200.0, current_price=400.0,
        ))

    def provider():
        raise scope.ScopeError("nobody is signed in")

    scope.set_scope_provider(provider)
    auth.set_authorizer(lambda headers, cookies: False)
    yield
    scope.set_scope_provider(None)
    auth.set_authorizer(None)


@pytest.fixture
def tokens(shared):
    with scope.using("user-a"):
        _, a = agent_tokens.issue("a-desktop")
    with scope.using("user-b"):
        _, b = agent_tokens.issue("b-desktop")
    return {"a": a, "b": b}


def headers(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client(shared):
    return TestClient(app)


def summary(client, token):
    response = client.post(
        "/api/agent/tools/get_portfolio_summary", json={}, headers=headers(token)
    )
    assert response.status_code == 200
    return response.json()["result"]


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------


def test_a_token_sees_only_its_own_account(client, tokens):
    a = summary(client, tokens["a"])
    b = summary(client, tokens["b"])

    assert [h["symbol"] for h in a["top_holdings"]] == ["AAPL"]
    assert [h["symbol"] for h in b["top_holdings"]] == ["TSLA"]
    assert a["total_value"] == pytest.approx(1500)
    assert b["total_value"] == pytest.approx(400)


def test_the_boundary_holds_on_every_tool(client, tokens):
    for tool in ("list_positions", "list_transactions", "find_data_gaps"):
        response = client.post(f"/api/agent/tools/{tool}", json={}, headers=headers(tokens["a"]))
        assert response.status_code == 200, tool
        assert "TSLA" not in response.text, tool


def test_one_account_cannot_read_anothers_position_by_name(client, tokens):
    response = client.post(
        "/api/agent/tools/get_position", json={"symbol": "TSLA"}, headers=headers(tokens["a"])
    )

    assert response.status_code == 400
    assert "No position found" in response.json()["detail"]


def test_the_markdown_context_is_scoped_too(client, tokens):
    body = client.get("/api/agent/context.md", headers=headers(tokens["a"])).text

    assert "AAPL" in body
    assert "TSLA" not in body


def test_a_revoked_token_stops_reaching_its_account(client, tokens):
    with scope.using("user-a"):
        record = agent_tokens.list_tokens()[0]
        agent_tokens.revoke(record["id"])

    assert client.get("/api/agent/tools", headers=headers(tokens["a"])).status_code == 401


def test_no_credential_is_refused_when_a_pack_authorizer_is_installed(client):
    """The authorizer says no to everything here; only a token gets in."""
    assert client.get("/api/agent/tools").status_code == 401


def test_a_forged_owner_segment_does_not_switch_accounts(client, tokens):
    secret = tokens["a"].split(".", 1)[1]
    forged = f"{agent_tokens.TOKEN_PREFIX}{agent_tokens._encode_owner('user-b')}.{secret}"

    assert client.get("/api/agent/tools", headers=headers(forged)).status_code == 401


def test_token_management_is_still_out_of_reach(client, tokens):
    assert client.get(
        "/api/settings/agent-tokens", headers=headers(tokens["a"])
    ).status_code == 403


# ---------------------------------------------------------------------------
# Remote MCP over the same boundary
# ---------------------------------------------------------------------------


def rpc(client, token, method, params=None, request_id=1):
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return client.post("/api/agent/mcp", json=message, headers=headers(token))


def test_remote_mcp_initializes(client, tokens):
    body = rpc(client, tokens["a"], "initialize", {"protocolVersion": "2025-06-18"}).json()

    assert body["result"]["serverInfo"]["name"] == "serin"
    assert body["result"]["protocolVersion"] == "2025-06-18"


def test_remote_mcp_lists_tools(client, tokens):
    body = rpc(client, tokens["a"], "tools/list", request_id=2).json()

    assert {t["name"] for t in body["result"]["tools"]} >= {"get_portfolio_summary", "get_position"}


def test_remote_mcp_calls_are_scoped_to_the_token(client, tokens):
    """The same boundary, through the protocol rather than the REST surface."""
    import json as jsonlib

    def totals(token):
        body = rpc(client, token, "tools/call",
                   {"name": "get_portfolio_summary", "arguments": {}}, request_id=3).json()
        assert body["result"]["isError"] is False
        return jsonlib.loads(body["result"]["content"][0]["text"])

    assert totals(tokens["a"])["total_value"] == pytest.approx(1500)
    assert totals(tokens["b"])["total_value"] == pytest.approx(400)


def test_remote_mcp_needs_a_token(client):
    response = client.post(
        "/api/agent/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )

    assert response.status_code == 401


def test_remote_mcp_answers_a_notification_with_202_and_no_body(client, tokens):
    response = client.post(
        "/api/agent/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=headers(tokens["a"]),
    )

    assert response.status_code == 202
    assert response.json() is None


def test_remote_mcp_reports_an_unknown_method(client, tokens):
    body = rpc(client, tokens["a"], "resources/list", request_id=9).json()

    assert body["error"]["code"] == -32601


def test_remote_mcp_surfaces_a_bad_argument_to_the_model(client, tokens):
    body = rpc(client, tokens["a"], "tools/call",
               {"name": "get_position", "arguments": {"ticker": "AAPL"}}, request_id=4).json()

    assert body["result"]["isError"] is True
    assert "Unknown argument" in body["result"]["content"][0]["text"]


# ---------------------------------------------------------------------------
# Concurrency
#
# The scope rides a ContextVar set per request. Sequential tests cannot see the
# failure that matters: two accounts in flight at once, one answered with the
# other's book. This drives the ASGI app directly so the requests genuinely
# overlap.
# ---------------------------------------------------------------------------


def test_interleaved_requests_never_cross_accounts(shared, tokens):
    import asyncio
    import json as jsonlib

    import httpx

    expected = {"a": 1500.0, "b": 400.0}

    async def rest(client, who):
        response = await client.post(
            "http://test/api/agent/tools/get_portfolio_summary",
            headers=headers(tokens[who]), json={},
        )
        return who, response.json()["result"]["total_value"]

    async def mcp(client, who):
        response = await client.post(
            "http://test/api/agent/mcp", headers=headers(tokens[who]),
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "get_portfolio_summary", "arguments": {}}},
        )
        payload = jsonlib.loads(response.json()["result"]["content"][0]["text"])
        return who, payload["total_value"]

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport) as client:
            jobs = []
            for _ in range(25):
                jobs += [rest(client, "a"), rest(client, "b"),
                         mcp(client, "a"), mcp(client, "b")]
            return await asyncio.gather(*jobs)

    results = asyncio.run(go())

    assert len(results) == 100
    assert [(who, value) for who, value in results if value != expected[who]] == []

"""The MCP server — protocol dispatch, and the whole chain end to end.

The protocol is implemented rather than imported, so it needs the same care an
SDK would have given it: version negotiation, notifications that get no reply,
unknown methods answered properly, and — the one that matters most for a model
— tool failures coming back as a *result* it can read and correct from, not a
transport error it never sees.
"""

from __future__ import annotations

import io
import json

import pytest
from backend import db, mcp_server
from backend.mcp_server import MCPServer, SerinClient
from backend.models import PositionIn


class FakeClient:
    """A SerinClient stand-in — no HTTP, records what it was asked."""

    def __init__(self, tools=None, result=None, error=None):
        self._tools = tools if tools is not None else [
            {"name": "get_portfolio_summary", "description": "totals", "inputSchema": {"type": "object"}}
        ]
        self._result = result if result is not None else {"total_value": 1500.0}
        self._error = error
        self.calls: list[tuple[str, dict]] = []

    def list_tools(self):
        if self._error:
            raise self._error
        return self._tools

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self._error:
            raise self._error
        return self._result


def request(method, params=None, request_id=1):
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def notification(method, params=None):
    message = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    return message


@pytest.fixture
def server():
    return MCPServer(client=FakeClient(), version="9.9.9")


# ---------------------------------------------------------------------------
# initialize
# ---------------------------------------------------------------------------


def test_initialize_advertises_tools(server):
    result = server.handle(request("initialize", {"protocolVersion": "2024-11-05"}))["result"]

    assert result["capabilities"]["tools"] is not None
    assert result["serverInfo"] == {"name": "serin", "version": "9.9.9"}
    assert "not financial advice" in result["instructions"]


@pytest.mark.parametrize("version", mcp_server.SUPPORTED_PROTOCOL_VERSIONS)
def test_a_supported_protocol_version_is_echoed(server, version):
    result = server.handle(request("initialize", {"protocolVersion": version}))["result"]

    assert result["protocolVersion"] == version


def test_an_unknown_protocol_version_falls_back_to_ours(server):
    result = server.handle(request("initialize", {"protocolVersion": "1999-01-01"}))["result"]

    assert result["protocolVersion"] == mcp_server.DEFAULT_PROTOCOL_VERSION


def test_initialize_without_params_still_works(server):
    result = server.handle(request("initialize"))["result"]

    assert result["protocolVersion"] == mcp_server.DEFAULT_PROTOCOL_VERSION


def test_initialized_notification_gets_no_reply(server):
    assert server.handle(notification("notifications/initialized")) is None


# ---------------------------------------------------------------------------
# tools/list and tools/call
# ---------------------------------------------------------------------------


def test_tools_list_forwards_the_instance_schemas(server):
    result = server.handle(request("tools/list"))["result"]

    assert result["tools"][0]["name"] == "get_portfolio_summary"


def test_tools_list_reports_a_transport_failure_as_an_error(server):
    server.client = FakeClient(error=RuntimeError("Serin is not running"))

    error = server.handle(request("tools/list"))["error"]

    assert error["code"] == mcp_server.INTERNAL_ERROR
    assert "not running" in error["message"]


def test_tools_call_returns_json_text_content(server):
    result = server.handle(
        request("tools/call", {"name": "get_portfolio_summary", "arguments": {}})
    )["result"]

    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"])["total_value"] == 1500.0


def test_tools_call_passes_arguments_through(server):
    server.handle(request("tools/call", {"name": "get_position", "arguments": {"symbol": "AAPL"}}))

    assert server.client.calls == [("get_position", {"symbol": "AAPL"})]


def test_tools_call_defaults_missing_arguments_to_empty(server):
    server.handle(request("tools/call", {"name": "get_portfolio_summary"}))

    assert server.client.calls == [("get_portfolio_summary", {})]


def test_a_failing_tool_is_a_result_the_model_can_read(server):
    """Not a JSON-RPC error: a protocol error never reaches the model, and the
    model is the only party that can fix a bad argument."""
    server.client = FakeClient(error=RuntimeError("Unknown argument: ticker"))

    response = server.handle(request("tools/call", {"name": "get_position", "arguments": {}}))

    assert "error" not in response
    assert response["result"]["isError"] is True
    assert "Unknown argument" in response["result"]["content"][0]["text"]


def test_tools_call_without_a_name_is_invalid_params(server):
    error = server.handle(request("tools/call", {"arguments": {}}))["error"]

    assert error["code"] == mcp_server.INVALID_PARAMS


def test_tools_call_with_non_object_arguments_is_invalid_params(server):
    error = server.handle(request("tools/call", {"name": "x", "arguments": ["nope"]}))["error"]

    assert error["code"] == mcp_server.INVALID_PARAMS


# ---------------------------------------------------------------------------
# Everything else
# ---------------------------------------------------------------------------


def test_ping_request_is_answered(server):
    assert server.handle(request("ping"))["result"] == {}


def test_ping_notification_is_not(server):
    assert server.handle(notification("ping")) is None


def test_unknown_method_is_method_not_found(server):
    error = server.handle(request("resources/list"))["error"]

    assert error["code"] == mcp_server.METHOD_NOT_FOUND
    assert "resources/list" in error["message"]


def test_unknown_notification_is_ignored(server):
    assert server.handle(notification("notifications/cancelled")) is None


# ---------------------------------------------------------------------------
# The stdio loop
# ---------------------------------------------------------------------------


def run_lines(lines, server):
    sink = io.StringIO()
    mcp_server.serve(stdin=io.StringIO("\n".join(lines)), stdout=sink, server=server)
    return [json.loads(line) for line in sink.getvalue().splitlines() if line.strip()]


def test_stdio_round_trip(server):
    replies = run_lines(
        [
            json.dumps(request("initialize", {"protocolVersion": "2024-11-05"}, request_id=1)),
            json.dumps(notification("notifications/initialized")),
            json.dumps(request("tools/list", request_id=2)),
        ],
        server,
    )

    assert [r["id"] for r in replies] == [1, 2]  # the notification produced nothing
    assert replies[1]["result"]["tools"][0]["name"] == "get_portfolio_summary"


def test_malformed_and_empty_lines_are_skipped_not_fatal(server):
    replies = run_lines(
        ["", "   ", "{not json", "[1, 2, 3]", json.dumps(request("ping", request_id=7))],
        server,
    )

    assert [r["id"] for r in replies] == [7]


# ---------------------------------------------------------------------------
# SerinClient
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def test_client_sends_the_bearer_token(monkeypatch):
    seen = {}

    def fake_request(method, url, headers=None, json=None, timeout=None):
        seen.update(method=method, url=url, headers=headers, json=json)
        return FakeResponse(200, {"tools": []})

    monkeypatch.setattr(mcp_server.httpx, "request", fake_request)

    SerinClient(base_url="http://serin.local/", token="serin_at_abc").list_tools()

    assert seen["url"] == "http://serin.local/api/agent/tools"
    assert seen["headers"]["authorization"] == "Bearer serin_at_abc"


def test_client_omits_the_header_when_there_is_no_token(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        mcp_server.httpx, "request",
        lambda method, url, headers=None, json=None, timeout=None: (
            seen.update(headers=headers) or FakeResponse(200, {"tools": []})
        ),
    )

    SerinClient(base_url="http://x", token="").list_tools()

    assert "authorization" not in seen["headers"]


def test_client_explains_a_401(monkeypatch):
    monkeypatch.setattr(
        mcp_server.httpx, "request",
        lambda *a, **k: FakeResponse(401, {"detail": "Locked"}),
    )

    with pytest.raises(RuntimeError, match="SERIN_AGENT_TOKEN"):
        SerinClient(base_url="http://x").list_tools()


def test_client_explains_a_403(monkeypatch):
    monkeypatch.setattr(
        mcp_server.httpx, "request",
        lambda *a, **k: FakeResponse(403, {"detail": "scoped"}),
    )

    with pytest.raises(RuntimeError, match="scoped elsewhere"):
        SerinClient(base_url="http://x").list_tools()


def test_client_surfaces_other_errors(monkeypatch):
    monkeypatch.setattr(
        mcp_server.httpx, "request",
        lambda *a, **k: FakeResponse(500, {"detail": "boom"}),
    )

    with pytest.raises(RuntimeError, match="500"):
        SerinClient(base_url="http://x").list_tools()


def test_client_reads_config_from_the_environment(monkeypatch):
    monkeypatch.setenv("SERIN_URL", "http://elsewhere:9000/")
    monkeypatch.setenv("SERIN_AGENT_TOKEN", "serin_at_env")

    client = SerinClient()

    assert client.base_url == "http://elsewhere:9000"
    assert client.token == "serin_at_env"


# ---------------------------------------------------------------------------
# End to end — MCP dispatch through the real HTTP surface and tool layer
# ---------------------------------------------------------------------------


@pytest.fixture
def live_server(tmp_path, monkeypatch):
    """An MCPServer whose HTTP calls land on the real app via TestClient.

    Proves the chain nobody else covers: MCP message → /api/agent → registry →
    the same functions the web UI uses.
    """
    from backend.config import settings
    from backend.main import app
    from fastapi.testclient import TestClient

    monkeypatch.setattr(settings, "auth_password", "")
    db.set_db_path(tmp_path / "e2e.db")
    db.init_db()
    db.create_position(PositionIn(
        symbol="MSFT", name="Microsoft", broker="fidelity", asset_type="stock",
        quantity=20, average_cost=200.0, current_price=300.0, sector="Technology",
    ))

    http = TestClient(app)

    def route(method, url, headers=None, json=None, timeout=None):
        path = url.replace("http://testserver", "")
        return http.request(method, path, headers=headers, json=json)

    monkeypatch.setattr(mcp_server.httpx, "request", route)
    return MCPServer(client=SerinClient(base_url="http://testserver"))


def test_end_to_end_tools_list(live_server):
    names = {t["name"] for t in live_server.handle(request("tools/list"))["result"]["tools"]}

    assert "get_portfolio_summary" in names
    assert "get_position" in names


def test_end_to_end_tool_call_returns_real_numbers(live_server):
    result = live_server.handle(
        request("tools/call", {"name": "get_portfolio_summary", "arguments": {}})
    )["result"]

    payload = json.loads(result["content"][0]["text"])
    assert result["isError"] is False
    assert payload["total_value"] == pytest.approx(6000)
    assert payload["top_holdings"][0]["symbol"] == "MSFT"


def test_end_to_end_bad_argument_reaches_the_model_as_a_readable_error(live_server):
    response = live_server.handle(
        request("tools/call", {"name": "get_position", "arguments": {"ticker": "MSFT"}})
    )

    assert response["result"]["isError"] is True
    assert "Unknown argument" in response["result"]["content"][0]["text"]


class ClosedPipe(io.StringIO):
    """A stdout whose reader has gone away — how an MCP client shuts a server
    down. Discovered live: this used to surface in the client's log as a
    BrokenPipeError traceback, which reads like a crash."""

    def write(self, _):
        raise BrokenPipeError(32, "Broken pipe")


def test_a_client_hanging_up_is_a_clean_exit(server):
    lines = "\n".join([
        json.dumps(request("ping", request_id=1)),
        json.dumps(request("ping", request_id=2)),
    ])

    mcp_server.serve(stdin=io.StringIO(lines), stdout=ClosedPipe(), server=server)
    # No exception: reaching this line is the assertion.


# ---------------------------------------------------------------------------
# LocalTools — the in-process source behind remote MCP
#
# The dispatch is written against "something with list_tools and call_tool", so
# serving MCP over HTTP costs a class rather than a second implementation of
# the protocol. These prove the two sources are interchangeable.
# ---------------------------------------------------------------------------


@pytest.fixture
def local(tmp_path):
    db.set_db_path(tmp_path / "local-tools.db")
    db.init_db()
    db.create_position(PositionIn(
        symbol="AMD", name="AMD", broker="fidelity", asset_type="stock",
        quantity=4, average_cost=100.0, current_price=250.0,
    ))
    return mcp_server.LocalTools()


def test_local_tools_lists_the_same_registry(local):
    from backend import tools

    assert {t["name"] for t in local.list_tools()} == {t.name for t in tools.all_tools()}


def test_local_tools_calls_without_http(local):
    assert local.call_tool("get_portfolio_summary", {})["total_value"] == pytest.approx(1000)


def test_local_tools_raises_for_a_bad_argument(local):
    from backend import tools

    with pytest.raises(tools.ToolError):
        local.call_tool("get_position", {"ticker": "AMD"})


def test_the_dispatcher_works_over_local_tools(local):
    server = MCPServer(client=local, version="1.2.3")

    listed = server.handle(request("tools/list"))["result"]["tools"]
    called = server.handle(
        request("tools/call", {"name": "get_portfolio_summary", "arguments": {}})
    )["result"]

    assert any(t["name"] == "get_portfolio_summary" for t in listed)
    assert called["isError"] is False
    assert json.loads(called["content"][0]["text"])["total_value"] == pytest.approx(1000)


def test_a_tool_failure_over_local_tools_still_reaches_the_model(local):
    server = MCPServer(client=local)

    result = server.handle(
        request("tools/call", {"name": "get_position", "arguments": {"ticker": "AMD"}})
    )["result"]

    assert result["isError"] is True
    assert "Unknown argument" in result["content"][0]["text"]

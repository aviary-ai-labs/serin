"""Serin as an MCP server — stdio transport, no new dependency.

Run it from an MCP client (Claude Desktop, Claude Code, anything that speaks
the protocol)::

    {
      "mcpServers": {
        "serin": {
          "command": "python",
          "args": ["-m", "backend.mcp_server"],
          "env": {
            "SERIN_URL": "http://127.0.0.1:8890",
            "SERIN_AGENT_TOKEN": "serin_at_…"
          }
        }
      }
    }

**A thin client, not a second copy of the app.** Tools are fetched from the
running instance's ``/api/agent`` surface and calls are forwarded to it, so
this process holds no portfolio logic, needs no database handle, and works
whether Serin runs on this machine, in Docker, or behind a reverse proxy. The
alternative — importing ``backend.tools`` directly — is simpler until the
first person runs Serin in a container, and then it is simply broken.

**The protocol is implemented here rather than pulled in.** MCP over stdio is
newline-delimited JSON-RPC 2.0 with a handful of methods; an SDK would be a
pinned dependency shipped to every self-hoster for about two hundred lines of
dispatch. It also keeps :class:`MCPServer` a pure message-in/message-out
object, which is why the tests need neither a subprocess nor a socket.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import httpx

# Versions this server knows how to speak. A client asking for one of these
# gets it back; anything else is answered with our newest, which is what the
# spec asks for — the client then decides whether it can live with that.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]

SERVER_NAME = "serin"

# JSON-RPC 2.0 reserved codes.
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class SerinClient:
    """HTTP access to a running Serin's agent surface."""

    def __init__(self, base_url: str | None = None, token: str | None = None, timeout: float = 30.0):
        self.base_url = (base_url or os.environ.get("SERIN_URL") or "http://127.0.0.1:8890").rstrip("/")
        self.token = token if token is not None else os.environ.get("SERIN_AGENT_TOKEN", "")
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = {"accept": "application/json"}
        if self.token:
            headers["authorization"] = f"Bearer {self.token}"
        return headers

    def _request(self, method: str, path: str, payload: dict | None = None) -> Any:
        url = f"{self.base_url}{path}"
        response = httpx.request(
            method, url, headers=self._headers(), json=payload, timeout=self.timeout
        )
        if response.status_code == 401:
            raise RuntimeError(
                "Serin refused the request (401). This instance has an app lock, so "
                "SERIN_AGENT_TOKEN must hold an agent token — create one under "
                "Settings → Agent tokens."
            )
        if response.status_code == 403:
            raise RuntimeError(
                "Serin refused the request (403). That credential is not an agent "
                "token, or is scoped elsewhere."
            )
        if response.status_code >= 400:
            detail = ""
            try:
                detail = str(response.json().get("detail") or "")
            except Exception:
                detail = response.text[:200]
            raise RuntimeError(f"Serin returned {response.status_code}: {detail}")
        return response.json()

    def list_tools(self) -> list[dict[str, Any]]:
        return list(self._request("GET", "/api/agent/tools").get("tools") or [])

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return self._request("POST", f"/api/agent/tools/{name}", arguments or {}).get("result")


class LocalTools:
    """The same two methods as :class:`SerinClient`, answered in-process.

    What the remote MCP endpoint uses. The dispatch in :class:`MCPServer` is
    written against "something with list_tools and call_tool", so serving MCP
    over HTTP costs a class rather than a second implementation of the
    protocol — and the protocol tests cover both transports.
    """

    def list_tools(self) -> list[dict[str, Any]]:
        from backend import tools

        return tools.describe()

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        from backend import tools

        return tools.call(name, arguments or {})


class MCPServer:
    """Message-in, message-out MCP dispatch. No I/O of its own."""

    def __init__(self, client: SerinClient | None = None, version: str = ""):
        self.client = client or SerinClient()
        self.version = version or _app_version()
        self.protocol_version = DEFAULT_PROTOCOL_VERSION
        self.initialized = False

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _result(request_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    @staticmethod
    def _tool_text(payload: Any, is_error: bool = False) -> dict[str, Any]:
        """A ``tools/call`` result.

        Tool failures come back as a *result* with ``isError`` set, not a
        JSON-RPC error: the model is the one that can fix a bad argument, and
        a protocol error never reaches it.
        """
        text = payload if isinstance(payload, str) else json.dumps(payload, indent=2, default=str)
        return {"content": [{"type": "text", "text": text}], "isError": is_error}

    # -- dispatch --------------------------------------------------------
    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Answer one message. Returns None for notifications, which by
        definition get no reply."""
        method = message.get("method") or ""
        request_id = message.get("id")
        params = message.get("params") or {}
        is_notification = "id" not in message

        if method == "initialize":
            requested = str(params.get("protocolVersion") or "")
            self.protocol_version = (
                requested if requested in SUPPORTED_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
            )
            self.initialized = True
            return self._result(
                request_id,
                {
                    "protocolVersion": self.protocol_version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": self.version},
                    "instructions": (
                        "Read-only access to the user's Serin portfolio. Figures are "
                        "already computed — quote them rather than recalculating from "
                        "rows. Check freshness fields before calling a price current, "
                        "and find_data_gaps before making confident claims about "
                        "returns. This is portfolio data, not financial advice."
                    ),
                },
            )

        if method in {"notifications/initialized", "initialized"}:
            return None

        if method == "ping":
            return None if is_notification else self._result(request_id, {})

        if method == "tools/list":
            try:
                return self._result(request_id, {"tools": self.client.list_tools()})
            except Exception as exc:
                return self._error(request_id, INTERNAL_ERROR, str(exc))

        if method == "tools/call":
            name = params.get("name")
            if not name:
                return self._error(request_id, INVALID_PARAMS, "tools/call requires a tool name")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                return self._error(request_id, INVALID_PARAMS, "arguments must be an object")
            try:
                return self._result(request_id, self._tool_text(self.client.call_tool(name, arguments)))
            except Exception as exc:
                # Surfaced to the model, not to the transport — see _tool_text.
                return self._result(request_id, self._tool_text(str(exc), is_error=True))

        if is_notification:
            return None  # unknown notifications are ignored, per the spec
        return self._error(request_id, METHOD_NOT_FOUND, f"Unknown method: {method}")


def _app_version() -> str:
    """Serin's version, without importing the whole app.

    This process is a client. Importing ``backend.config`` would drag in
    settings, the database path and the plugin loader for a version string,
    and would fail on a machine that only has the MCP client installed.
    """
    try:
        from backend.config import APP_VERSION

        return APP_VERSION
    except Exception:
        return "0.0.0"


def serve(stdin=None, stdout=None, server: MCPServer | None = None) -> None:
    """The stdio loop: one JSON message per line, in and out."""
    source = stdin or sys.stdin
    sink = stdout or sys.stdout
    server = server or MCPServer()
    for line in source:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            # No id to answer against — a malformed line cannot be replied to
            # coherently, so it is dropped rather than guessed at.
            continue
        if not isinstance(message, dict):
            continue
        response = server.handle(message)
        if response is None:
            continue
        try:
            sink.write(json.dumps(response) + "\n")
            sink.flush()
        except BrokenPipeError:
            # The client hung up mid-answer. That is how an MCP client shuts a
            # server down, so it is a normal exit, not a crash — and a
            # traceback here would land in the client's log looking like one.
            return


def main() -> None:  # pragma: no cover - process entry point
    serve()


if __name__ == "__main__":  # pragma: no cover
    main()

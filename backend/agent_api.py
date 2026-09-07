"""HTTP surface for AI agents — the tool layer, plus token management.

Two routers with deliberately different reach:

``/api/agent/*``
    The tool surface. Everything here is read-only, and an agent token from
    :mod:`backend.agent_tokens` is scoped to exactly this prefix. FastAPI
    publishes it in ``/openapi.json`` alongside the rest of the app, which is
    the cheap half of "integrate with an agent": frameworks that speak OpenAPI
    need no MCP server at all.

``/api/settings/agent-tokens``
    Issuing and revoking those tokens. Deliberately *not* under ``/api/agent``
    — a credential must never be able to mint or list its own successors, and
    the scope check is a prefix test, so the separation has to be visible in
    the path rather than argued about. Session-only, and covered by a test.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from backend import agent_tokens, tools
from backend.config import APP_VERSION
from backend.mcp_server import LocalTools, MCPServer


async def agent_scope(request: Request):
    """Run the request as the token's owner.

    The single highest-risk thing in the agent surface. On Cloud the scope
    cannot come from the request context — an agent client has no session and
    no cookie — so it has to come from the credential, before any query runs.
    Getting this wrong serves one customer another's portfolio, so it is a
    dependency on the whole router rather than a line each endpoint remembers.

    A request authorized some other way (a signed-in browser hitting the same
    URLs) is left alone: the pack's own provider already knows who it is.
    """
    from backend import auth, scope

    record = auth.agent_token_record(request.headers)
    if record is None:
        yield None
        return
    with scope.using(str(record.get("owner") or scope.LOCAL_SCOPE)) as owner:
        yield owner


agent_router = APIRouter(prefix="/api/agent", tags=["agent"], dependencies=[Depends(agent_scope)])
token_router = APIRouter(prefix="/api/settings/agent-tokens", tags=["agent"])


# ---------------------------------------------------------------------------
# Tool surface
# ---------------------------------------------------------------------------


@agent_router.get("")
async def agent_manifest() -> dict[str, Any]:
    """What this agent surface is and what it can do — the entry point a
    client hits before deciding which tool to call."""
    return {
        "name": "serin",
        "version": APP_VERSION,
        "description": (
            "Read-only access to one Serin portfolio: holdings, returns, "
            "realised gains, transactions, price history and data-quality gaps."
        ),
        "read_only": True,
        "tool_count": len(tools.all_tools()),
        "tools": [tool.name for tool in tools.all_tools()],
    }


@agent_router.get("/tools")
async def list_agent_tools() -> dict[str, Any]:
    """Every tool with its JSON schema, in MCP ``tools/list`` shape so the MCP
    server can forward this verbatim and the two surfaces cannot drift."""
    return {"tools": tools.describe()}


@agent_router.post("/tools/{tool_name}")
async def call_agent_tool(tool_name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run one tool. Arguments are the JSON body.

    A bad tool name or bad arguments is a 400 carrying the message the model
    should read and correct from — not a 500, and not a silent empty result.
    """
    if tools.get(tool_name) is None:
        raise HTTPException(404, f"Unknown tool: {tool_name}")
    try:
        result = await asyncio.to_thread(tools.call, tool_name, arguments or {})
    except tools.ToolError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"tool": tool_name, "result": result}


@agent_router.post("/mcp")
async def mcp_endpoint(message: dict[str, Any], response: Response) -> Any:
    """Model Context Protocol over HTTP — one JSON-RPC message per request.

    The remote counterpart to ``python -m backend.mcp_server``: a client that
    speaks streamable HTTP points at this URL with an agent token and needs no
    local process at all. Deliberately stateless — every request builds its own
    dispatcher — because the only state the protocol asks a tool server to keep
    is the negotiated version, and re-negotiating per request costs nothing and
    survives a restart, a second worker, and a load balancer.

    Notifications get 202 with no body, per the spec: they have no id, so there
    is nothing to answer.
    """
    server = MCPServer(client=LocalTools(), version=APP_VERSION)
    answer = await asyncio.to_thread(server.handle, message)
    if answer is None:
        response.status_code = 202
        return None
    return answer


@agent_router.get("/context.md", response_class=PlainTextResponse)
async def agent_context() -> str:
    """The portfolio as a Markdown briefing sheet.

    The zero-integration path: every agent can read text, including ones with
    no tool support at all, and a person can paste it into any chat window.
    Cheap to serve and cheap to keep correct, because it is assembled from the
    same tools as everything else.
    """
    return await asyncio.to_thread(_render_context)


def _money(value: Any) -> str:
    return f"{value:,.2f}" if isinstance(value, (int, float)) else "—"


def _render_context() -> str:
    summary = tools.call("get_portfolio_summary", {"top_holdings": 10})
    lines: list[str] = [
        "# Portfolio snapshot",
        "",
        f"- **Total value:** {_money(summary['total_value'])}",
        f"- **Total cost:** {_money(summary['total_cost'])}",
        f"- **Unrealised gain:** {_money(summary['total_gain'])} "
        f"({summary['total_gain_pct']}%)",
        f"- **Cash:** {_money(summary['cash_value'])}",
        f"- **Positions:** {summary['position_count']}",
    ]

    freshness = summary.get("freshness") or {}
    if freshness.get("prices_updated_at"):
        lines.append(f"- **Prices last updated:** {freshness['prices_updated_at']}")
    if freshness.get("stale_positions"):
        lines.append(
            f"- **Stale prices:** {freshness['stale_positions']} holding(s) older than "
            f"{freshness.get('stale_after_hours')}h — {', '.join(freshness.get('stale_symbols') or [])}"
        )

    holdings = summary.get("top_holdings") or []
    if holdings:
        lines += ["", "## Largest holdings", "", "| Symbol | Value | Weight | Unrealised |", "| --- | ---: | ---: | ---: |"]
        lines += [
            f"| {h['symbol']} | {_money(h['market_value'])} | {h['weight_pct']}% | "
            f"{_money(h['unrealized_gain'])} ({h['unrealized_gain_pct']}%) |"
            for h in holdings
        ]

    # Performance reads the price cache and can legitimately have nothing to
    # say on a fresh install. A missing section beats a fabricated zero.
    try:
        performance = tools.call("get_performance")
    except tools.ToolError:
        performance = None
    if performance and performance.get("periods"):
        lines += ["", "## Returns", "", "| Period | Return |", "| --- | ---: |"]
        lines += [f"| {row['period']} | {row['return_pct']}% |" for row in performance["periods"]]
        if performance.get("note"):
            lines += ["", f"> {performance['note']}"]

    try:
        gaps = tools.call("find_data_gaps")
    except tools.ToolError:
        gaps = None
    if gaps and gaps.get("gaps"):
        lines += ["", "## Data gaps", ""]
        lines += [
            f"- **{gap.get('severity', 'info')}** — {gap.get('title') or gap.get('code')}"
            for gap in gaps["gaps"][:10]
        ]
        lines += ["", f"> {gaps['note']}"]

    lines += [
        "",
        "---",
        "",
        "*Generated by Serin. Read-only portfolio data — not financial advice.*",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Token management (session-only)
# ---------------------------------------------------------------------------


class AgentTokenIn(BaseModel):
    name: str = ""
    scope: str = agent_tokens.READ_SCOPE


def _refuse_agent_credentials(request: Request) -> None:
    """Belt and braces alongside the middleware's scope check.

    The middleware already refuses an agent token outside ``/api/agent``, so
    this is unreachable today. It is here because the consequence of that
    prefix test ever being loosened is a token that can mint more tokens, and
    a second, local check costs nothing.
    """
    from backend import auth

    if auth.agent_token_record(request.headers) is not None:
        raise HTTPException(403, "Agent tokens cannot manage agent tokens.")


@token_router.get("")
async def list_agent_tokens(request: Request) -> dict[str, Any]:
    _refuse_agent_credentials(request)
    from backend import scope

    return {
        "tokens": agent_tokens.list_tokens(),
        "multiuser": scope.provider_installed(),
        "scopes": list(agent_tokens.SCOPES),
    }


@token_router.post("")
async def create_agent_token(request: Request, body: AgentTokenIn) -> dict[str, Any]:
    _refuse_agent_credentials(request)
    try:
        record, plaintext = agent_tokens.issue(body.name, body.scope)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # The only time the plaintext is ever returned. Stored hashed, so it
    # cannot be shown again and a leaked backup is not a working credential.
    return {**record, "token": plaintext}


@token_router.delete("/{token_id}")
async def delete_agent_token(request: Request, token_id: str) -> dict[str, Any]:
    _refuse_agent_credentials(request)
    if not agent_tokens.revoke(token_id):
        raise HTTPException(404, "No such agent token")
    return {"ok": True}

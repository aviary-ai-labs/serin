"""The agent tool layer — one registry, several surfaces.

Every way an AI agent reaches Serin resolves to this registry: the MCP server
(:mod:`backend.mcp_server`), the OpenAPI surface at ``/api/agent`` and, out of
tree, the Intelligence pack's chat loop. Writing the tools once is the point —
a tool added here appears on all of them, and the awkward parts (schemas,
argument validation, the read-only guarantee) are solved in one place instead
of three.

**Read-only by construction.** :func:`register` refuses a tool that claims
otherwise, so "an agent cannot mutate the portfolio" is a property of the
registry rather than a convention every future tool has to remember. A
hallucinated position edit corrupts cost basis — the one number the whole
product exists to get right — so the guarantee is worth enforcing structurally
and worth a test.

**Tools answer questions; they do not return tables.** Language models are
poor at arithmetic over many rows, and Serin already computes TWR, XIRR,
Modified Dietz and FIFO-matched realized gains correctly in Python. A tool
that hands back forty positions invites the model to sum them itself and be
subtly wrong; a tool that hands back the answer cannot be. Row-level tools
exist for when an agent genuinely wants rows, but the summary tools lead.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


class ToolError(Exception):
    """A tool call that cannot be served — unknown name, bad arguments, or a
    handler that failed. Carries a message meant for the model to read."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Any] = field(repr=False)
    read_only: bool = True

    def to_mcp(self) -> dict[str, Any]:
        """This tool as an MCP ``tools/list`` entry."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


_REGISTRY: dict[str, Tool] = {}


def register(tool: Tool) -> Tool:
    if not tool.read_only:
        raise ValueError(
            f"Tool {tool.name!r} is not read-only. The agent surface is read-only by "
            "construction; a write tool needs its own reviewed path, not this registry."
        )
    if tool.name in _REGISTRY:
        raise ValueError(f"Tool {tool.name!r} is already registered")
    _REGISTRY[tool.name] = tool
    return tool


def all_tools() -> list[Tool]:
    return [_REGISTRY[name] for name in sorted(_REGISTRY)]


def get(name: str) -> Tool | None:
    return _REGISTRY.get(name)


def describe() -> list[dict[str, Any]]:
    """The whole toolset, MCP-shaped. Also what ``GET /api/agent/tools``
    serves, so the two surfaces cannot drift."""
    return [tool.to_mcp() for tool in all_tools()]


# --------------------------------------------------------------------------
# Argument validation
#
# Deliberately not a JSON Schema library. The schemas here are small and
# hand-written, a validator is a dependency this project would have to pin and
# ship to self-hosters, and the failure mode that actually matters is a model
# passing "2024" where a number belongs — which needs coercion, not a spec.
# --------------------------------------------------------------------------

_TYPE_NAMES = {
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _coerce(name: str, value: Any, spec: dict[str, Any]) -> Any:
    expected = spec.get("type")
    if expected is None or value is None:
        return value
    # Models routinely send numbers as strings ("2024") and booleans as
    # "true". Rejecting those is technically correct and practically useless.
    if expected in {"number", "integer"} and isinstance(value, str):
        try:
            return int(value) if expected == "integer" else float(value)
        except ValueError:
            raise ToolError(f"{name!r} must be a {expected}; got {value!r}") from None
    if expected == "boolean" and isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
        raise ToolError(f"{name!r} must be a boolean; got {value!r}")
    if expected == "number" and isinstance(value, bool):
        raise ToolError(f"{name!r} must be a number; got a boolean")
    python_type = _TYPE_NAMES.get(expected)
    if python_type and not isinstance(value, python_type):
        raise ToolError(f"{name!r} must be a {expected}; got {type(value).__name__}")
    return value


def validate_arguments(tool: Tool, arguments: dict[str, Any] | None) -> dict[str, Any]:
    """Check and coerce a call's arguments against the tool's schema.

    Unknown keys are an error rather than ignored: silently dropping one turns
    "you asked for 2024" into a full-history answer the model then reports as
    the year's, which is worse than a message it can correct from.
    """
    supplied = dict(arguments or {})
    properties: dict[str, Any] = tool.input_schema.get("properties") or {}
    required: list[str] = list(tool.input_schema.get("required") or [])

    unknown = sorted(set(supplied) - set(properties))
    if unknown:
        known = ", ".join(sorted(properties)) or "none"
        raise ToolError(
            f"Unknown argument{'s' if len(unknown) > 1 else ''} for {tool.name}: "
            f"{', '.join(unknown)}. Accepted: {known}."
        )
    missing = [key for key in required if supplied.get(key) in (None, "")]
    if missing:
        raise ToolError(f"{tool.name} requires {', '.join(missing)}.")

    cleaned: dict[str, Any] = {}
    for key, value in supplied.items():
        spec = properties.get(key) or {}
        coerced = _coerce(key, value, spec)
        allowed = spec.get("enum")
        if allowed and coerced not in allowed:
            raise ToolError(f"{key!r} must be one of {', '.join(map(str, allowed))}; got {coerced!r}")
        cleaned[key] = coerced
    return cleaned


def call(name: str, arguments: dict[str, Any] | None = None) -> Any:
    """Run a tool by name. Raises :class:`ToolError` for anything a caller can
    fix, so surfaces can turn it into one consistent message."""
    tool = get(name)
    if tool is None:
        known = ", ".join(sorted(_REGISTRY)) or "none registered"
        raise ToolError(f"Unknown tool {name!r}. Available: {known}.")
    cleaned = validate_arguments(tool, arguments)
    try:
        return tool.handler(**cleaned)
    except ToolError:
        raise
    except Exception as exc:  # a handler blowing up is a tool failure, not a 500
        raise ToolError(f"{tool.name} failed: {type(exc).__name__}: {exc}") from exc


# Importing the module registers the built-in tools. Kept at the bottom so the
# registry above is fully defined when portfolio.py imports from it.
from backend.tools import portfolio as _portfolio  # noqa: E402,F401

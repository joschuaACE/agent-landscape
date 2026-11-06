"""Loopback MCP bridge: lends one agent's landscape toolbox to a local ``claude -p`` run (``claude-code/*``).

Why: the Claude Code CLI runs its *own* tool loop and never hands OpenAI ``tool_calls`` back. So instead of the
CLI's built-in tools (Bash/Edit outside our sandbox), it gets exactly one MCP server – this endpoint – whose tools
are the agent's toolbox. Every call still goes through ``Agent._run_tool`` → ``Toolbox.call``: profile tool access,
sandboxes, secret protection, 🔧 events, usage records and the step budget stay ours.

Transport: MCP "streamable HTTP" in its simplest form (POST JSON-RPC → JSON response, no SSE stream, no session
header). Each agent turn gets a random single-use token in the URL; the session is closed when the turn ends.
Only loopback clients are served (the provider itself is loopback-only).
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

SERVER_NAME = "landscape"  # → tools appear to Claude Code as mcp__landscape__<tool>
PROTOCOL_VERSION = "2025-06-18"
BUDGET_TEXT = "Tool budget exhausted – give your final answer now with the information you have."
_ERROR_PREFIXES = ("Tool error", "Invalid tool arguments", "Tool budget exhausted")


@dataclass
class BridgeSession:
    token: str
    url: str
    specs: Callable[[], list[dict[str, Any]]]  # OpenAI function specs, re-read on every tools/list
    run: Callable[[str, dict[str, Any]], Awaitable[str]]  # executes one tool call for the agent
    max_steps: int
    agent: str = ""
    steps: int = 0
    calls: list[str] = field(default_factory=list)

    def tools(self) -> list[dict[str, Any]]:
        out = []
        for spec in self.specs():
            fn = spec.get("function") or {}
            if fn.get("name"):
                out.append({"name": fn["name"], "description": fn.get("description") or "",
                            "inputSchema": fn.get("parameters") or {"type": "object", "properties": {}}})
        return out


class MCPBridge:
    """Registry of open bridge sessions + the JSON-RPC handler behind ``POST /mcp/bridge/{token}``."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.sessions: dict[str, BridgeSession] = {}

    def open(self, specs: Callable[[], list[dict[str, Any]]], run: Callable[[str, dict[str, Any]], Awaitable[str]],
             *, max_steps: int, agent: str = "") -> BridgeSession:
        token = secrets.token_urlsafe(24)
        session = BridgeSession(token=token, url=f"{self.base_url}/mcp/bridge/{token}", specs=specs, run=run,
                                max_steps=max(1, max_steps), agent=agent)
        self.sessions[token] = session
        return session

    def close(self, session: BridgeSession | None) -> None:
        if session is not None:
            self.sessions.pop(session.token, None)

    def get(self, token: str) -> BridgeSession | None:
        return self.sessions.get(token)

    async def handle(self, session: BridgeSession, payload: Any) -> Any:
        """JSON-RPC request(s) → response(s); None when the payload held only notifications (HTTP 202)."""
        if isinstance(payload, list):
            replies = [r for r in [await self._one(session, m) for m in payload] if r is not None]
            return replies or None
        return await self._one(session, payload)

    async def _one(self, s: BridgeSession, msg: Any) -> dict[str, Any] | None:
        if not isinstance(msg, dict) or "method" not in msg:
            return None  # responses / garbage from the client: nothing to answer
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:  # notification (e.g. notifications/initialized)
            return None
        try:
            result = await self._dispatch(s, str(method), params if isinstance(params, dict) else {})
        except _RpcError as exc:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": exc.code, "message": str(exc)}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    async def _dispatch(self, s: BridgeSession, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return {"protocolVersion": str(params.get("protocolVersion") or PROTOCOL_VERSION),
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "agent-landscape", "version": "1"},
                    "instructions": "Agent Landscape mission tools for this agent (sandboxed, logged)."}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": s.tools()}
        if method == "tools/call":
            return await self._call(s, str(params.get("name") or ""), params.get("arguments") or {})
        raise _RpcError(-32601, f"Method not found: {method}")

    async def _call(self, s: BridgeSession, name: str, args: Any) -> dict[str, Any]:
        if name not in {t["name"] for t in s.tools()}:
            return _text(f"Tool error: unknown tool '{name}'", error=True)
        if not isinstance(args, dict):
            return _text("Invalid tool arguments: arguments must be an object", error=True)
        if s.steps >= s.max_steps:
            return _text(BUDGET_TEXT, error=True)
        s.steps += 1
        s.calls.append(name)
        try:
            result = await s.run(name, args)
        except Exception as exc:  # noqa: BLE001 – a failing tool must not kill the CLI's loop
            log.warning("bridge tool %s failed for %s: %s", name, s.agent, exc)
            return _text(f"Tool error: {exc}", error=True)
        return _text(result, error=result.startswith(_ERROR_PREFIXES))


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _text(text: str, *, error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": error}

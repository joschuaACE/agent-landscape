"""MCP tools via the LiteLLM proxy's MCP gateway (``/mcp-rest``).

All MCP servers – and REST APIs that LiteLLM converted from an OpenAPI spec – are managed in
LiteLLM (permissions per key/team, logging, spend). Agent Landscape just lists and calls them.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import Settings

log = logging.getLogger(__name__)

# heuristics: tools that change state need an explicit opt-in (never auto-selected with their server)
_WRITE_RE = re.compile(r"(^|[_\-.])(write|delete|remove|move|rename|patch|append|update|create|insert|put|post|"
                       r"execute|exec|run|send|upload|drop|set|commit|push|merge|deploy)([_\-.]|$)", re.I)


@dataclass
class MCPTool:
    server: str
    server_id: str
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    write: bool = False

    @property
    def ref(self) -> str:
        return f"{self.server}/{self.name}"

    def brief(self) -> dict[str, Any]:
        return {"ref": self.ref, "server": self.server, "name": self.name, "write": self.write,
                "description": (self.description or "")[:300],
                "params": list((self.input_schema or {}).get("properties", {}))}


class MCPGateway:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._cache: tuple[float, list[MCPTool], dict[str, Any]] | None = None

    @property
    def available(self) -> bool:
        return bool(self.settings.proxy_url)

    def _headers(self) -> dict[str, str]:
        k = self.settings.proxy_key
        return {"Authorization": f"Bearer {k}"} if k else {}

    async def catalog(self, refresh: bool = False) -> tuple[list[MCPTool], dict[str, Any]]:
        """(tools, server status) – cached for 60 s."""
        if not self.available:
            return [], {}
        if self._cache and not refresh and time.time() - self._cache[0] < 60:
            return self._cache[1], self._cache[2]
        try:
            async with httpx.AsyncClient(timeout=20) as http:
                r = await http.get(f"{self.settings.proxy_url}/mcp-rest/tools/list", headers=self._headers())
                r.raise_for_status()
                data = r.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not list MCP tools from LiteLLM: %s", exc)
            return [], {"_error": str(exc)}
        tools = []
        for t in data.get("tools", []):
            info = t.get("mcp_info") or {}
            ann = t.get("annotations") or {}
            write = not ann.get("readOnlyHint", False) and bool(_WRITE_RE.search(t.get("name", ""))) \
                or bool(ann.get("destructiveHint"))
            tools.append(MCPTool(server=info.get("server_name") or "mcp", server_id=info.get("server_id") or "",
                                 name=t.get("name", ""), description=t.get("description") or "",
                                 input_schema=t.get("inputSchema") or {"type": "object", "properties": {}},
                                 write=write))
        status = data.get("server_outcomes") or {}
        self._cache = (time.time(), tools, status)
        return tools, status

    async def select(self, refs: list[str]) -> list[MCPTool]:
        """Expand selections: 'server' → its read-only tools, 'server/tool' → that tool (incl. write tools)."""
        tools, _ = await self.catalog()
        out: dict[str, MCPTool] = {}
        for ref in refs:
            server, _, name = ref.partition("/")
            for t in tools:
                if t.server == server and ((name and t.name == name) or (not name and not t.write)):
                    out[t.ref] = t
        return list(out.values())

    async def call(self, tool: MCPTool, arguments: dict[str, Any]) -> str:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10)) as http:
            r = await http.post(f"{self.settings.proxy_url}/mcp-rest/tools/call", headers=self._headers(),
                                json={"name": tool.name, "arguments": arguments, "server_id": tool.server_id})
        if r.status_code >= 400:
            detail = r.text[:500]
            raise RuntimeError(f"MCP call failed ({r.status_code}): {detail}")
        data = r.json()
        parts = []
        for c in data.get("content") or []:
            if c.get("type") == "text":
                parts.append(c.get("text", ""))
            elif c.get("type") == "resource":
                res = c.get("resource") or {}
                parts.append(res.get("text") or json.dumps(res)[:2000])
            else:
                parts.append(json.dumps(c)[:2000])
        text = "\n".join(parts) if parts else json.dumps(data.get("structuredContent") or data)[:20000]
        if data.get("isError"):
            raise RuntimeError(text[:1000])
        return text

"""claude-code/* tool calls via the loopback MCP bridge: JSON-RPC handler, CLI argv, and a full mission where a
fake `claude` CLI acts as MCP client and calls the agent's real toolbox."""

from __future__ import annotations

import asyncio
import json
import socket
import stat
import sys
import threading
import time

import httpx
import pytest
import uvicorn

from agent_landscape.claude_code import build_argv
from agent_landscape.config import Settings
from agent_landscape.context.mcp_bridge import BUDGET_TEXT, MCPBridge
from agent_landscape.web.app import create_app

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="fake CLI uses a POSIX shebang")

SPECS = [{"type": "function", "function": {"name": "read_file", "description": "Read a file",
                                           "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                                                          "required": ["path"]}}}]


def _rpc(mid, method, params=None):
    return {"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}


async def test_bridge_jsonrpc_lifecycle_and_budget() -> None:
    calls: list[tuple[str, dict]] = []

    async def run(name, args):
        calls.append((name, args))
        return f"content of {args['path']}"

    bridge = MCPBridge("http://127.0.0.1:9")
    s = bridge.open(lambda: SPECS, run, max_steps=2, agent="Engineer")
    assert s.url == f"http://127.0.0.1:9/mcp/bridge/{s.token}" and bridge.get(s.token) is s

    init = await bridge.handle(s, _rpc(1, "initialize", {"protocolVersion": "2025-03-26"}))
    assert init["result"]["protocolVersion"] == "2025-03-26" and "tools" in init["result"]["capabilities"]
    assert await bridge.handle(s, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    tools = (await bridge.handle(s, _rpc(2, "tools/list")))["result"]["tools"]
    assert tools == [{"name": "read_file", "description": "Read a file", "inputSchema": SPECS[0]["function"]["parameters"]}]

    ok = (await bridge.handle(s, _rpc(3, "tools/call", {"name": "read_file", "arguments": {"path": "a.md"}})))["result"]
    assert ok == {"content": [{"type": "text", "text": "content of a.md"}], "isError": False}
    bad = (await bridge.handle(s, _rpc(4, "tools/call", {"name": "rm_rf", "arguments": {}})))["result"]
    assert bad["isError"] and "unknown tool" in bad["content"][0]["text"]
    await bridge.handle(s, _rpc(5, "tools/call", {"name": "read_file", "arguments": {"path": "b.md"}}))
    over = (await bridge.handle(s, _rpc(6, "tools/call", {"name": "read_file", "arguments": {"path": "c.md"}})))
    assert over["result"]["isError"] and over["result"]["content"][0]["text"] == BUDGET_TEXT
    assert [a["path"] for _, a in calls] == ["a.md", "b.md"]  # unknown tool + over-budget never ran

    assert (await bridge.handle(s, _rpc(7, "resources/list")))["error"]["code"] == -32601
    batch = await bridge.handle(s, [_rpc(8, "ping"), {"jsonrpc": "2.0", "method": "notifications/x"}])
    assert batch == [{"jsonrpc": "2.0", "id": 8, "result": {}}]
    bridge.close(s)
    assert bridge.get(s.token) is None


def test_argv_with_bridge_allows_only_landscape_tools() -> None:
    argv = build_argv("claude", "claude-code/sonnet", "sys", bridge_url="http://127.0.0.1:8800/mcp/bridge/tok")
    assert argv[argv.index("--tools") + 1] == ""  # still no built-in Bash/Edit/Read
    assert argv[argv.index("--allowedTools") + 1] == "mcp__landscape"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[-2] == "--mcp-config" and "--strict-mcp-config" in argv
    assert json.loads(argv[-1]) == {"mcpServers": {"landscape": {"type": "http",
                                                                 "url": "http://127.0.0.1:8800/mcp/bridge/tok"}}}
    plain = build_argv("claude", "claude-code/sonnet", "sys")
    assert "--allowedTools" not in plain and plain[-1] == '{"mcpServers":{}}'


# ------------------------------------------------------------------ e2e: fake CLI = MCP client
FAKE = """#!{python}
import json, sys, urllib.request
sys.stdin.read()
cfg = json.loads(sys.argv[sys.argv.index("--mcp-config") + 1])
server = cfg["mcpServers"].get("landscape")
text = "plain answer"
if server:
    def rpc(mid, method, params=None):
        body = json.dumps({{"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {{}}}}).encode()
        req = urllib.request.Request(server["url"], body, {{"Content-Type": "application/json",
                                                             "Accept": "application/json, text/event-stream"}})
        return json.loads(urllib.request.urlopen(req, timeout=10).read())
    rpc(1, "initialize", {{"protocolVersion": "2025-06-18"}})
    names = [t["name"] for t in rpc(2, "tools/list")["result"]["tools"]]
    if "read_file" in names:
        out = rpc(3, "tools/call", {{"name": "read_file", "arguments": {{"path": "notes.md"}}}})["result"]
        text = "tool said: " + out["content"][0]["text"]
    else:
        text = "tools: " + ",".join(names)
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": text,
                  "total_cost_usd": 0.001, "usage": {{"input_tokens": 3, "output_tokens": 4}}}}))
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def server(tmp_path):
    fake = tmp_path / "fake-claude"
    fake.write_text(FAKE.format(python=sys.executable))
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "notes.md").write_text("BRIDGE-MARKER-42\n")
    port = _free_port()
    settings = Settings(port=port, mock=False, mock_delay=(0, 0), default_model="claude-code/sonnet",
                        claude_code_enabled=True, claude_code_bin=str(fake), context_roots=[str(tmp_path)])
    app = create_app(settings)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    deadline = time.time() + 10
    while not srv.started and time.time() < deadline:
        time.sleep(0.05)
    yield app, settings.public_url, docs
    srv.should_exit = True
    th.join(timeout=5)


async def test_claude_code_agent_uses_its_toolbox_through_the_bridge(server):
    app, base, docs = server
    rt = app.state.runtime
    async with httpx.AsyncClient(timeout=60) as http:
        assert (await http.post(f"{base}/mcp/bridge/nope", json=_rpc(1, "ping"))).status_code == 404
        body = {"goal": "Summarise the notes", "title": "Bridge", "mode": "hierarchy",
                "context": [{"kind": "path", "path": str(docs)}]}
        topic_id = (await http.post(f"{base}/api/missions", json=body)).json()["id"]
        for _ in range(400):
            t = rt.topics.get(topic_id)
            if t and t.status != "active" and t.root_agent_id:
                break
            await asyncio.sleep(0.05)
        assert t.status == "completed", t.result
        report = (await http.get(f"{base}/api/topics/{topic_id}/report")).json()
    arts = [a for r in report["rounds"] for a in r["artifacts"]]
    used = [(a["agent_name"], c) for a in arts for c in a["tool_calls"] if c["name"] == "read_file"]
    assert used, [(a["agent_name"], a["output"][:80]) for a in arts]
    assert all(c["ok"] and "BRIDGE-MARKER-42" in c["result"] for _, c in used)  # real toolbox, real sandbox
    assert any("tool said:" in a["output"] and "BRIDGE-MARKER-42" in a["output"] for a in arts)
    assert rt.mcp_bridge.sessions == {}  # every turn closed its session

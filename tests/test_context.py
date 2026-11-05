"""Unit tests: read-only sandbox, A2A context encoding, toolbox dispatch."""

from __future__ import annotations

import json

import pytest

from agent_landscape.a2a.types import DataPart, FilePart, Message, TextPart
from agent_landscape.context import sandbox
from agent_landscape.context.intel import ContextItem, Doc, MissionIntel, items_from_message, items_to_parts
from agent_landscape.context.mcp import MCPGateway, is_write_tool
from agent_landscape.context.toolbox import Toolbox
from agent_landscape.config import Settings


@pytest.fixture()
def repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def hello():\n    return 'NEEDLE in app'\n")
    (tmp_path / "README.md").write_text("# Demo\nSee src/app.py\n")
    (tmp_path / ".env").write_text("API_KEY=SUPER_SECRET\nNEEDLE=leak\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("NEEDLE in node_modules")
    (tmp_path / "logo.bin").write_bytes(b"\x00\x01binary")
    return tmp_path


@pytest.mark.parametrize("name,write", [
    ("get_commit", False), ("list_issues", False), ("pull_request_read", False), ("issue_read", False),
    ("search_code", False), ("ui_get", False), ("vault_read", False), ("tavily_search", False),
    ("add_issue_comment", True), ("assign_copilot_to_issue", True), ("fork_repository", True),
    ("request_copilot_review", True), ("create_pull_request", True), ("merge_pull_request", True),
    ("push_files", True), ("issue_write", True), ("vault_delete", True), ("command_execute", True),
])
def test_mcp_write_tool_heuristic(name, write):
    assert is_write_tool(name) is write


def test_mcp_write_tool_annotations_win():
    assert is_write_tool("add_note", {"readOnlyHint": True}) is False
    assert is_write_tool("get_thing", {"readOnlyHint": False}) is True
    assert is_write_tool("get_thing", {"readOnlyHint": True, "destructiveHint": True}) is True


def test_check_allowed_enforces_roots_and_secrets(repo, tmp_path_factory):
    assert sandbox.check_allowed(str(repo), [str(repo)]) == repo.resolve()
    other = tmp_path_factory.mktemp("other")
    with pytest.raises(sandbox.SandboxError, match="outside"):
        sandbox.check_allowed(str(other), [str(repo)])
    with pytest.raises(sandbox.SandboxError, match="outside"):
        sandbox.check_allowed(str(repo / ".." / other.name), [str(repo)])
    with pytest.raises(sandbox.SandboxError, match="hidden"):
        sandbox.check_allowed(str(repo / ".env"), [str(repo)])
    with pytest.raises(sandbox.SandboxError, match="does not exist"):
        sandbox.check_allowed(str(repo / "nope"), [str(repo)])
    info = sandbox.inspect(repo)
    assert info["kind"] == "dir" and info["files"] == 3  # app.py, README.md, logo.bin – no .env / node_modules


def test_file_sandbox_hides_secrets_and_noise(repo):
    fs = sandbox.FileSandbox([repo])
    listing = fs.list_dir()
    assert "src/" in listing and "README.md" in listing
    assert ".env" not in listing and "node_modules" not in listing
    assert "NEEDLE in app" in fs.read_file("src/app.py")
    hits = fs.search("needle")
    assert "src/app.py:2" in hits and "SUPER_SECRET" not in hits and "node_modules" not in hits and ".env" not in hits
    for bad in (".env", "../", "/etc/passwd", "node_modules/x.js"):
        with pytest.raises(sandbox.SandboxError):
            fs.read_file(bad)
    with pytest.raises(sandbox.SandboxError, match="binary"):
        fs.read_file("logo.bin")


def test_context_roundtrip_through_a2a_parts():
    items = [ContextItem(kind="file", name="notes.md", content="hello äöü", mime="text/markdown"),
             ContextItem(kind="path", path="~/project"), ContextItem(kind="url", url="https://example.com"),
             ContextItem(kind="tool", tool="tavily_search_mcp")]
    parts = items_to_parts("Do the thing", items)
    assert isinstance(parts[0], TextPart) and isinstance(parts[1], FilePart) and isinstance(parts[2], DataPart)
    msg = Message(role="user", parts=parts)
    wire = Message.model_validate(json.loads(json.dumps(msg.dump())))  # through JSON like over A2A
    assert wire.text == "Do the thing"  # attachments never leak into the instruction text
    back = items_from_message(wire)
    assert [i.kind for i in back] == ["file", "path", "url", "tool"]
    assert back[0].content == "hello äöü" and back[0].name == "notes.md" and back[3].tool == "tavily_search_mcp"


async def test_toolbox_dispatch(repo):
    intel = MissionIntel(roots=[repo])
    intel.add_doc(Doc(name="spec.md", text="x" * 20000, source="file"))
    tb = Toolbox(intel, MCPGateway(Settings(mock=True)), mock=True)
    names = {s["function"]["name"] for s in tb.specs()}
    assert names == {"read_intel", "list_dir", "read_file", "search_files"}
    out, ok, _ = await tb.call("read_intel", {"name": "spec.md", "offset": 0, "limit": 1000})
    assert ok and "offset=1000" in out
    out, ok, _ = await tb.call("read_file", json.dumps({"path": ".env"}))
    assert not ok and "SUPER_SECRET" not in out
    out, ok, _ = await tb.call("nope", {})
    assert not ok and "not available" in out
    out, ok, _ = await tb.call("search_files", "not json")
    assert not ok and "Invalid tool arguments" in out
    assert "spec.md" in intel.briefing() and "read_intel" in intel.briefing()

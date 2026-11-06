"""Local claude-code/* provider (slice 1) – driven against a fake `claude` CLI; no real binary is exercised."""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from agent_landscape.config import Settings
from agent_landscape.llm import LLM

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="fake CLI uses a POSIX shebang")

MESSAGES = [
    {"role": "system", "content": "You are a test agent."},
    {"role": "user", "content": "Say hi."},
]

FAKE = """#!{python}
import json, os, sys, time
from pathlib import Path

stdin = sys.stdin.read()
Path(__file__).with_name("record.json").write_text(json.dumps({{
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
    "stdin": stdin,
    "has_api_key": "ANTHROPIC_API_KEY" in os.environ,
}}))
mode = os.environ.get("FAKE_MODE", "ok")
if mode == "sleep":
    time.sleep(30)
print(json.dumps({{
    "type": "result",
    "is_error": mode == "error",
    "result": "boom" if mode == "error" else "hi from fake claude",
    "total_cost_usd": 0.0123,
    "usage": {{"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100,
              "cache_creation_input_tokens": 7}},
}}))
"""


@pytest.fixture()
def fake_cli(tmp_path: Path) -> Path:
    path = tmp_path / "fake-claude"
    path.write_text(FAKE.format(python=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _record(fake: Path) -> dict | None:
    rec = fake.with_name("record.json")
    return json.loads(rec.read_text()) if rec.exists() else None


def _llm(fake: Path, **overrides) -> LLM:
    values = dict(mock=False, claude_code_enabled=True, claude_code_bin=str(fake))
    values.update(overrides)
    return LLM(Settings(**values))


async def test_ac1_complete_returns_fake_result(fake_cli: Path) -> None:
    res = await _llm(fake_cli).complete(MESSAGES, model="claude-code/sonnet")
    assert res.text == "hi from fake claude"
    assert res.tokens == 10 + 5 + 100 + 7
    assert res.cost == 0.0
    assert res.notional_cost == pytest.approx(0.0123)
    assert res.tool_calls == []
    assert res.model == "claude-code/sonnet"


async def test_ac2_argv_cwd_and_env_are_locked_down(fake_cli: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    await _llm(fake_cli).complete(MESSAGES, model="claude-code/sonnet")
    rec = _record(fake_cli)
    assert rec is not None
    argv = rec["argv"]
    assert argv[argv.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--system-prompt") + 1] == "You are a test agent."
    assert "Say hi." in rec["stdin"]
    cwd = Path(rec["cwd"]).resolve()
    assert cwd != Path(os.getcwd()).resolve()
    assert cwd != fake_cli.parent.resolve()
    assert not cwd.exists()  # temp dir is cleaned up after the call
    assert rec["has_api_key"] is False


async def test_ac3_disabled_provider_refuses(fake_cli: Path) -> None:
    with pytest.raises(RuntimeError, match="CLAUDE_CODE_ENABLED"):
        await _llm(fake_cli, claude_code_enabled=False).complete(MESSAGES, model="claude-code/sonnet")
    assert _record(fake_cli) is None


async def test_ac4_non_loopback_host_needs_override(fake_cli: Path) -> None:
    with pytest.raises(RuntimeError, match="local-only"):
        await _llm(fake_cli, host="0.0.0.0").complete(MESSAGES, model="claude-code/sonnet")
    assert _record(fake_cli) is None
    res = await _llm(fake_cli, host="0.0.0.0", claude_code_allow_remote=True).complete(
        MESSAGES, model="claude-code/sonnet")
    assert res.text == "hi from fake claude"


async def test_ac5_timeout_kills_the_cli(fake_cli: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "sleep")
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        await _llm(fake_cli, claude_code_timeout=1).complete(MESSAGES, model="claude-code/sonnet")
    assert time.monotonic() - start < 3


async def test_ac5_is_error_raises(fake_cli: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "error")
    with pytest.raises(RuntimeError, match="boom"):
        await _llm(fake_cli).complete(MESSAGES, model="claude-code/sonnet")


async def test_ac6_mock_mode_never_starts_the_cli(fake_cli: Path) -> None:
    llm = LLM(Settings(mock=True, mock_delay=(0, 0), claude_code_enabled=True, claude_code_bin=str(fake_cli)))
    res = await llm.complete(MESSAGES, model="claude-code/sonnet", mock="mocked answer")
    assert res.text == "mocked answer"
    assert res.cost == 0.0
    assert _record(fake_cli) is None
    assert llm._claude_code is None


async def test_ac7_models_listed_resolved_and_checked(fake_cli: Path) -> None:
    llm = _llm(fake_cli)
    assert "claude-code/sonnet" in await llm.list_models()
    proxied = _llm(fake_cli, proxy_url="http://127.0.0.1:9")
    assert proxied.resolve_model("claude-code/sonnet") == "claude-code/sonnet"
    assert proxied.resolve_model("gpt-4o-mini") == "litellm_proxy/gpt-4o-mini"
    assert await llm.check_models() == []
    problems = await _llm(Path("/nonexistent")).check_models()
    assert any("not found" in p for p in problems)

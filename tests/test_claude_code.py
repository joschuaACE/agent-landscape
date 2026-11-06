"""Local claude-code/* provider – driven against a fake `claude` CLI plus a fixture of real CLI 2.1.311 output.
The real binary is only exercised by the opt-in `test_live_claude_cli` (CLAUDE_CODE_LIVE=1)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import stat
import sys
import time
from pathlib import Path

import pytest

from agent_landscape.claude_code import (DEFAULT_SYSTEM, MAX_SYSTEM_ARG, SMOKE_MESSAGES, _parse, build_argv,
                                         served_model, to_result)
from agent_landscape.config import Settings
from agent_landscape.llm import LLM

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="fake CLI uses a POSIX shebang")

FIXTURE = Path(__file__).parent / "fixtures" / "claude_p_result.json"

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
    "leaked": sorted(k for k in os.environ if k.startswith(("ANTHROPIC_", "CLAUDE_CODE_USE_"))
                     or k == "CLAUDE_CODE_BIN"),
    "autoupdater": os.environ.get("DISABLE_AUTOUPDATER"),
    "pid": os.getpid(),
}}))
mode = os.environ.get("FAKE_MODE", "ok")
if mode == "sleep":
    time.sleep(30)
failed = mode in ("error", "exit1")
print(json.dumps({{
    "type": "result",
    "subtype": "error_during_execution" if failed else "success",
    "is_error": failed,
    "api_error_status": 429 if mode == "apierror" else None,
    "result": "boom" if failed else "hi from fake claude",
    "total_cost_usd": 0.0123,
    "usage": {{"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100,
              "cache_creation_input_tokens": 7}},
}}))
sys.exit(1 if mode == "exit1" else 0)
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


async def _wait_for_record(fake: Path, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            rec = _record(fake)
        except ValueError:  # half-written
            rec = None
        if rec:
            return rec
        await asyncio.sleep(0.05)
    raise AssertionError("fake CLI never started")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _fixture(**changes) -> dict:
    data = json.loads(FIXTURE.read_text())
    data.update(changes)
    return data


def _llm(fake: Path, **overrides) -> LLM:
    values = dict(mock=False, claude_code_enabled=True, claude_code_bin=str(fake))
    values.update(overrides)
    return LLM(Settings(**values))


async def test_ac1_complete_returns_fake_result(fake_cli: Path) -> None:
    res = await _llm(fake_cli).complete(MESSAGES, model="claude-code/sonnet")
    assert res.text == "hi from fake claude"
    assert res.tokens == 10 + 5  # input + output only
    assert res.cache_read_tokens == 100
    assert res.cache_write_tokens == 7
    assert res.cost == 0.0
    assert res.notional_cost == pytest.approx(0.0123)
    assert res.tool_calls == []
    assert res.model == "claude-code/sonnet"


async def test_ac2_argv_cwd_and_env_are_locked_down(fake_cli: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("ANTHROPIC_MODEL", "opus")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("CLAUDE_CODE_BIN", "/our/own/setting")
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
    assert rec["leaked"] == []
    assert rec["autoupdater"] == "1"
    assert argv[argv.index("--setting-sources") + 1] == "project"  # blocks ~/.claude/CLAUDE.md + settings
    assert "--no-session-persistence" in argv
    assert "--bare" not in argv  # --bare is API-key auth only
    assert argv[-2:] == ["--mcp-config", '{"mcpServers":{}}']  # variadic flag must stay last


async def test_empty_system_prompt_gets_a_default(fake_cli: Path) -> None:
    await _llm(fake_cli).complete([{"role": "user", "content": "Say hi."}], model="claude-code/sonnet")
    argv = _record(fake_cli)["argv"]
    assert argv[argv.index("--system-prompt") + 1] == DEFAULT_SYSTEM


async def test_huge_system_prompt_goes_to_stdin(fake_cli: Path) -> None:
    big = "x" * (MAX_SYSTEM_ARG + 10)
    await _llm(fake_cli).complete([{"role": "system", "content": big}, {"role": "user", "content": "Hi"}],
                                  model="claude-code/sonnet")
    rec = _record(fake_cli)
    assert len(rec["argv"][rec["argv"].index("--system-prompt") + 1]) < 1000
    assert big in rec["stdin"] and rec["stdin"].index(big) < rec["stdin"].index("Hi")


async def test_setting_sources_configurable_but_never_dropped(fake_cli: Path) -> None:
    await _llm(fake_cli, claude_code_setting_sources="local").complete(MESSAGES, model="claude-code/sonnet")
    argv = _record(fake_cli)["argv"]
    assert argv[argv.index("--setting-sources") + 1] == "local"
    blank = build_argv("claude", "claude-code/sonnet", "s", "  ")
    assert blank[blank.index("--setting-sources") + 1] == "project"  # empty must NOT mean the CLI default


def test_setting_sources_env_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SETTING_SOURCES", "")
    assert Settings.from_env(db_path=None).claude_code_setting_sources == "project"


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
    rec = _record(fake_cli)
    assert rec is not None and not _alive(rec["pid"])


async def test_cancel_kills_the_cli(fake_cli: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "sleep")
    task = asyncio.create_task(_llm(fake_cli).complete(MESSAGES, model="claude-code/sonnet"))
    pid = (await _wait_for_record(fake_cli))["pid"]
    assert _alive(pid)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not _alive(pid), "cancelled completion left an orphaned claude process"


async def test_ac5_is_error_raises(fake_cli: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "error")
    with pytest.raises(RuntimeError, match="boom") as exc:
        await _llm(fake_cli).complete(MESSAGES, model="claude-code/sonnet")
    assert "error_during_execution" in str(exc.value)


async def test_nonzero_exit_surfaces_the_json_error(fake_cli: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "exit1")
    with pytest.raises(RuntimeError, match="boom"):
        await _llm(fake_cli).complete(MESSAGES, model="claude-code/sonnet")


async def test_api_error_status_raises_even_without_is_error(fake_cli: Path,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "apierror")
    with pytest.raises(RuntimeError, match="api_error_status=429"):
        await _llm(fake_cli).complete(MESSAGES, model="claude-code/sonnet")


def test_real_cli_fixture_maps_to_result(caplog: pytest.LogCaptureFixture) -> None:
    stdout = "some CLI notice\n" + json.dumps(_fixture())  # real CLI prints one line of JSON
    with caplog.at_level(logging.DEBUG, logger="agent_landscape.claude_code"):
        res = to_result(_parse(stdout), "claude-code/sonnet")
    assert res.text == "Hi! How can I help?"
    assert res.tokens == 102  # 2 input + 100 output (owner decision, round 4)
    assert (res.cache_read_tokens, res.cache_write_tokens) == (649, 1873)
    assert res.cost == 0.0
    assert res.notional_cost == pytest.approx(0.0085609)
    assert res.model == "claude-code/sonnet"
    assert served_model(_fixture()) == "claude-sonnet-5-5"
    assert "claude-sonnet-5-5" in caplog.text


def test_permission_denials_only_warn(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="agent_landscape.claude_code"):
        res = to_result(_fixture(permission_denials=[{"tool_name": "Bash"}]), "claude-code/sonnet")
    assert res.text and "permission denials" in caplog.text


def test_cache_tokens_do_not_feed_xp_or_totals() -> None:
    from agent_landscape.agents.base import Agent, _task_usage

    added: list[tuple] = []
    fake = type("A", (), {})()
    fake.tokens, fake.cost, fake.topic_id = 0, 0.0, "t"
    xp: list[int] = []
    fake.gain_xp = xp.append
    fake.runtime = type("R", (), {"add_usage": lambda self, *a, **k: added.append(a)})()
    usage = {"tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "cost": 0.0, "model": None,
             "topic_id": "t"}
    tok = _task_usage.set(usage)
    try:
        Agent._account(fake, to_result(_fixture(), "claude-code/sonnet"))
    finally:
        _task_usage.reset(tok)
    assert (usage["tokens"], usage["cache_read_tokens"], usage["cache_write_tokens"]) == (102, 649, 1873)
    assert fake.tokens == 102 and xp == [102 // 20] and added == [(102, 0.0)]


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


@pytest.mark.skipif(os.getenv("CLAUDE_CODE_LIVE") != "1" or shutil.which("claude") is None,
                    reason="live smoke test: set CLAUDE_CODE_LIVE=1 and have a logged-in `claude` on PATH")
async def test_live_claude_cli() -> None:
    """The only test that proves --system-prompt works on the real binary (uses the seat: one haiku call)."""
    llm = LLM(Settings(mock=False, claude_code_enabled=True, host="127.0.0.1", claude_code_timeout=120))
    res = await llm.complete(SMOKE_MESSAGES, model="claude-code/haiku")
    assert "PONG" in res.text
    assert res.cost == 0.0
    assert res.tokens > 0

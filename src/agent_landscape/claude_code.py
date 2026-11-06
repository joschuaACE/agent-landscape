"""Local, opt-in ``claude-code/*`` provider: runs the user's own ``claude`` CLI in print mode.

Why: Claude Code enterprise/team seats are subscription logins, not API keys, so they can't sit behind
LiteLLM (which only hosts official, key-based routes). This runner shells out to the locally logged-in
CLI instead. It is a *personal-seat* workaround: loopback hosts only, no tools, no MCP, no repo access,
no user-level ``~/.claude`` settings or CLAUDE.md.
Flags verified against the real CLI 2.1.311 (``claude --help`` + tests/fixtures/claude_p_result.json); they are
still a moving target – re-run ``python -m agent_landscape.claude_code`` after CLI upgrades.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import signal
import sys
import tempfile
from typing import Any

from .config import Settings
from .llm import LLMResult

log = logging.getLogger(__name__)

PREFIX = "claude-code/"
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
# Stripped by prefix (exact-name lists go stale): API keys / base URLs / model overrides and Bedrock/Vertex
# switches would silently move billing off the subscription seat.
_STRIPPED_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_USE_")
# our own settings – the CLI reads CLAUDE_CODE_* itself, so don't hand it ours
_OWN_ENV = ("CLAUDE_CODE_ENABLED", "CLAUDE_CODE_BIN", "CLAUDE_CODE_MODELS", "CLAUDE_CODE_TIMEOUT",
            "CLAUDE_CODE_CONCURRENCY", "CLAUDE_CODE_ALLOW_REMOTE", "CLAUDE_CODE_SETTING_SOURCES")
# keep the flag set stable while the server runs; update the CLI by hand and re-run the smoke test
_CHILD_ENV_SET = {"DISABLE_AUTOUPDATER": "1"}
# an empty --system-prompt may count as "unset" and bring back the default Claude Code prompt
DEFAULT_SYSTEM = "You are a helpful assistant."
# one argv string is capped (Linux 128 KiB, macOS ~1 MiB total) – longer system prompts go to stdin
MAX_SYSTEM_ARG = 100_000
_STDERR_SNIPPET = 500
SMOKE_MESSAGES = [
    {"role": "system", "content": "Reply with exactly the word PONG."},
    {"role": "user", "content": "ping"},
]


def is_loopback(host: str) -> bool:
    return host.strip().lower() in LOOPBACK_HOSTS


def remote_problem(settings: Settings) -> str | None:
    """Why the provider must not run on this host (None = fine). A seat is personal, not a shared service."""
    if is_loopback(settings.host) or settings.claude_code_allow_remote:
        return None
    return (f"claude-code/* is a personal-seat, local-only provider, but HOST={settings.host} is not loopback. "
            "Bind to 127.0.0.1 or set CLAUDE_CODE_ALLOW_REMOTE=1 if you really mean it.")


def _text(content: Any) -> str:
    """OpenAI content may be a string or a list of parts – keep only the text."""
    if isinstance(content, list):
        return "\n".join(str(p.get("text", "")) if isinstance(p, dict) else str(p) for p in content)
    return "" if content is None else str(content)


def build_prompt(messages: list[dict[str, Any]]) -> tuple[str, str]:
    """(system prompt, stdin transcript). Tool turns become plain text – the CLI has no tool protocol here."""
    system: list[str] = []
    lines: list[str] = []
    for m in messages:
        role = m.get("role", "user")
        text = _text(m.get("content"))
        if role == "system":
            system.append(text)
            continue
        if role == "tool":
            lines.append(f"[tool result {m.get('tool_call_id', '')}]:\n{text}")
            continue
        calls = m.get("tool_calls") or []
        parts = [text] if text else []
        for tc in calls:
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            parts.append(f"[called tool {fn.get('name', '?')}({fn.get('arguments', '')})]")
        lines.append(f"[{role}]:\n" + "\n".join(parts))
    return "\n\n".join(system), "\n\n".join(lines)


def split_system(system: str, transcript: str) -> tuple[str, str]:
    """Keep argv small: an oversized system prompt moves in front of the stdin transcript."""
    system = system.strip() or DEFAULT_SYSTEM
    if len(system.encode()) <= MAX_SYSTEM_ARG:
        return system, transcript
    head = "Follow the instructions in the [system instructions] block of the input as your system prompt."
    return head, f"[system instructions]:\n{system}\n\n{transcript}"


def build_argv(bin_: str, model: str, system: str, setting_sources: str = "project") -> list[str]:
    """The exact CLI invocation. NOTE: Claude Code CLI flags are a moving target – re-check on CLI upgrades.

    - ``--tools ''`` disables all built-in tools; ``--strict-mcp-config`` + an empty ``--mcp-config`` loads
      no MCP servers.
    - ``--setting-sources project`` (cwd = empty temp dir) skips ``~/.claude/CLAUDE.md`` and
      ``~/.claude/settings.json`` (env, apiKeyHelper, hooks) but keeps the seat login. Managed policy applies.
      Never omitted: the CLI default would load user files.
    - ``--no-session-persistence``: no transcripts of agent prompts pile up under ``~/.claude/projects``.
    - ``--mcp-config`` is variadic, so it must stay last.
    """
    return [bin_, "-p", "--output-format", "json", "--model", model.removeprefix(PREFIX),
            "--system-prompt", system or DEFAULT_SYSTEM, "--tools", "",
            "--setting-sources", setting_sources.strip() or "project", "--no-session-persistence",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']


def child_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(_STRIPPED_PREFIXES) and k not in _OWN_ENV}
    env.update(_CHILD_ENV_SET)
    return env


def _parse(stdout: str) -> dict[str, Any]:
    """The final JSON object on stdout (tolerates log lines before it and the verbose array form)."""
    candidates = [stdout.strip()] + [ln.strip() for ln in reversed(stdout.splitlines()) if ln.strip().startswith(("{", "["))]
    for raw in candidates:
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if isinstance(data, list):
            data = next((d for d in reversed(data) if isinstance(d, dict) and d.get("type") == "result"), None)
        if isinstance(data, dict):
            return data
    raise ValueError("no JSON object on stdout")


def served_model(data: dict[str, Any]) -> str | None:
    """The model that actually answered (alias ``sonnet`` → e.g. ``claude-sonnet-5-5``); None if unreported."""
    usage = data.get("modelUsage")
    if not isinstance(usage, dict) or not usage:
        return None
    name, info = next(iter(usage.items()))
    if isinstance(info, dict) and info.get("canonicalModel"):
        return str(info["canonicalModel"])
    return str(name)


def raise_for_error(data: dict[str, Any], stderr: str = "") -> None:
    """The CLI can exit 0 yet report a failed turn (``is_error``) or an upstream HTTP error (``api_error_status``)."""
    status = data.get("api_error_status")
    if not data.get("is_error") and status in (None, "", 0):
        return
    detail = str(data.get("result") or "").strip()[:_STDERR_SNIPPET]
    raise RuntimeError(f"claude CLI reported an error (subtype={data.get('subtype')}, api_error_status={status}): "
                       f"{detail} {stderr.strip()[-_STDERR_SNIPPET:]}".rstrip())


def to_result(data: dict[str, Any], model: str) -> LLMResult:
    """Map the CLI's result JSON to an LLMResult; ``model`` stays the requested ``claude-code/<alias>``.

    Token rule (owner decision, round 4): ``tokens`` = input + output only. Prompt-cache traffic is the
    CLI's own scaffold, reported separately as ``cache_read_tokens`` / ``cache_write_tokens`` and never
    counted into totals or XP. Cost is 0 (flat subscription); ``total_cost_usd`` is only notional.
    """
    usage = data.get("usage") or {}
    if data.get("permission_denials"):
        log.warning("claude CLI reported permission denials: %s", data["permission_denials"])
    log.debug("claude-code %s served by %s", model, served_model(data))
    return LLMResult(
        text=str(data.get("result") or ""), cost=0.0, model=model, tool_calls=[],
        tokens=int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0),
        cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
        cache_write_tokens=int(usage.get("cache_creation_input_tokens") or 0),
        notional_cost=float(data.get("total_cost_usd") or 0.0))


async def _kill(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the CLI's whole process group and reap it – an orphaned ``claude`` would keep the seat busy."""
    if proc.returncode is None:
        try:
            if sys.platform != "win32":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError):
            pass
    await proc.wait()


class ClaudeCodeRunner:
    """Runs one ``claude -p`` subprocess per completion, bounded by a semaphore (a seat has rate limits)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._sem = asyncio.Semaphore(max(1, settings.claude_code_concurrency))
        self._warned_tools = False
        self._warned_user = False

    async def complete(self, messages: list[dict[str, Any]], model: str,
                       tools: list[dict[str, Any]] | None = None) -> LLMResult:
        s = self.settings
        problem = remote_problem(s)
        if problem:
            raise RuntimeError(problem)
        if tools and not self._warned_tools:
            self._warned_tools = True
            log.info("Landscape tools are not supported on claude-code/* (slice 1) – answering without tools")
        return to_result(await self.run_json(messages, model), model)

    def argv_for(self, messages: list[dict[str, Any]], model: str) -> tuple[list[str], str]:
        s = self.settings
        if "user" in [p.strip() for p in s.claude_code_setting_sources.split(",")] and not self._warned_user:
            self._warned_user = True
            log.warning("CLAUDE_CODE_SETTING_SOURCES=%s loads ~/.claude settings and CLAUDE.md into agents",
                        s.claude_code_setting_sources)
        system, transcript = split_system(*build_prompt(messages))
        return build_argv(s.claude_code_bin, model, system, s.claude_code_setting_sources), transcript

    async def run_json(self, messages: list[dict[str, Any]], model: str) -> dict[str, Any]:
        """Run the CLI once and return its (error-checked) result JSON."""
        argv, transcript = self.argv_for(messages, model)
        async with self._sem:  # queueing doesn't count against the timeout (that starts inside _run)
            stdout, stderr, code = await self._run(argv, transcript)
        try:
            data = _parse(stdout)
        except ValueError:
            snippet = (stderr.strip() or stdout.strip())[-_STDERR_SNIPPET:]
            if code != 0:
                raise RuntimeError(f"claude CLI exited with code {code}: {snippet}") from None
            raise RuntimeError(f"claude CLI returned no valid JSON: {snippet}") from None
        raise_for_error(data, stderr)  # the CLI often exits 1 *with* an error JSON – prefer its message
        if code != 0:
            raise RuntimeError(f"claude CLI exited with code {code}: {stderr.strip()[-_STDERR_SNIPPET:]}")
        return data

    async def _run(self, argv: list[str], stdin: str) -> tuple[str, str, int]:
        """Fresh temp cwd: the CLI can't write into any repo, and with ``--setting-sources project`` it loads
        no user CLAUDE.md, no user settings and no project files. Managed policy still applies.
        Own process group, killed on timeout *and* cancel (Recall, server stop) – no orphans on the seat."""
        timeout = self.settings.claude_code_timeout
        with tempfile.TemporaryDirectory(prefix="al-claude-code-") as cwd:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv, cwd=cwd, env=child_env(), stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    start_new_session=sys.platform != "win32")
            except FileNotFoundError:
                raise RuntimeError(f"claude CLI not found: {argv[0]} (set CLAUDE_CODE_BIN)") from None
            except OSError as exc:  # e.g. E2BIG: argument list too long
                raise RuntimeError(f"claude CLI could not be started: {exc}") from None
            try:
                out, err = await asyncio.wait_for(proc.communicate(stdin.encode()), timeout)
            except asyncio.TimeoutError:
                await _kill(proc)
                raise TimeoutError(f"claude CLI did not answer within {timeout}s (CLAUDE_CODE_TIMEOUT)") from None
            except BaseException:  # CancelledError included
                await asyncio.shield(_kill(proc))
                raise
        return out.decode(errors="replace"), err.decode(errors="replace"), proc.returncode or 0


def _shown_argv(argv: list[str]) -> str:
    shown = list(argv)
    i = shown.index("--system-prompt") + 1
    if len(shown[i]) > 60:
        shown[i] = shown[i][:57] + "..."
    return shlex.join(shown)


async def _smoke(model: str) -> int:
    """One real ping through the runner; prints what a review needs. Exit 0 = OK, 1 = no PONG, 2 = error."""
    settings = Settings.from_env(claude_code_enabled=True, mock=False)
    runner = ClaudeCodeRunner(settings)
    argv, _ = runner.argv_for(SMOKE_MESSAGES, model)
    print("argv:   ", _shown_argv(argv))
    try:
        data = await runner.run_json(SMOKE_MESSAGES, model)
    except Exception as exc:  # noqa: BLE001 – manual diagnostics
        print("RESULT:  ERROR", exc)
        return 2
    res = to_result(data, model)
    ok = "PONG" in res.text
    print("text:   ", res.text.strip()[:200])
    print("tokens: ", res.tokens, f"(cache read {res.cache_read_tokens} / write {res.cache_write_tokens})")
    print("cost:   ", res.cost, f"(notional ${res.notional_cost:.4f})")
    print("model:  ", served_model(data), "| duration_ms:", data.get("duration_ms"))
    print("RESULT: ", "OK" if ok else "FAILED (no PONG – is --system-prompt ignored?)")
    return 0 if ok else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG if "-v" in sys.argv else logging.WARNING)
    alias = next((a for a in sys.argv[1:] if not a.startswith("-")), "haiku")
    sys.exit(asyncio.run(_smoke(PREFIX + alias.removeprefix(PREFIX))))

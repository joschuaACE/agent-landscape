"""Local, opt-in ``claude-code/*`` provider: runs the user's own ``claude`` CLI in print mode.

Why: Claude Code enterprise/team seats are subscription logins, not API keys, so they can't sit behind
LiteLLM (which only hosts official, key-based routes). This runner shells out to the locally logged-in
CLI instead. It is a *personal-seat* workaround: loopback hosts only, no tools, no MCP, no repo access.
Prototype (slice 1) – the CLI flags used here are a moving target; see ``build_argv``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from typing import Any

from .config import Settings
from .llm import LLMResult

log = logging.getLogger(__name__)

PREFIX = "claude-code/"
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
# removed from the child env so the CLI uses its own subscription login and never silently bills an API key
_STRIPPED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")
_STDERR_SNIPPET = 500


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


def build_argv(bin_: str, model: str, system: str) -> list[str]:
    """The exact CLI invocation. NOTE: Claude Code CLI flags are a moving target – re-check on CLI upgrades.

    ``--tools ''`` disables all built-in tools; ``--strict-mcp-config`` + an empty ``--mcp-config``
    loads no MCP servers. ``--mcp-config`` is variadic, so it must stay last.
    """
    return [bin_, "-p", "--output-format", "json", "--model", model.removeprefix(PREFIX),
            "--system-prompt", system, "--tools", "", "--strict-mcp-config",
            "--mcp-config", '{"mcpServers":{}}']


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in _STRIPPED_ENV:
        env.pop(key, None)
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


def to_result(data: dict[str, Any], model: str) -> LLMResult:
    usage = data.get("usage") or {}
    tokens = sum(int(usage.get(k) or 0) for k in
                 ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    # cost 0: the seat is a flat subscription; the CLI's dollar figure is only what the API *would* cost
    return LLMResult(text=str(data.get("result") or ""), tokens=tokens, cost=0.0, model=model,
                     notional_cost=float(data.get("total_cost_usd") or 0.0), tool_calls=[])


class ClaudeCodeRunner:
    """Runs one ``claude -p`` subprocess per completion, bounded by a semaphore (a seat has rate limits)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._sem = asyncio.Semaphore(max(1, settings.claude_code_concurrency))
        self._warned_tools = False

    async def complete(self, messages: list[dict[str, Any]], model: str,
                       tools: list[dict[str, Any]] | None = None) -> LLMResult:
        s = self.settings
        problem = remote_problem(s)
        if problem:
            raise RuntimeError(problem)
        if tools and not self._warned_tools:
            self._warned_tools = True
            log.info("Landscape tools are not supported on claude-code/* (slice 1) – answering without tools")
        system, transcript = build_prompt(messages)
        argv = build_argv(s.claude_code_bin, model, system)
        async with self._sem:
            stdout, stderr, code = await self._run(argv, transcript)
        snippet = stderr.strip()[-_STDERR_SNIPPET:]
        if code != 0:
            raise RuntimeError(f"claude CLI exited with code {code}: {snippet or stdout.strip()[-_STDERR_SNIPPET:]}")
        try:
            data = _parse(stdout)
        except ValueError:
            raise RuntimeError(f"claude CLI returned no valid JSON: {stdout.strip()[-_STDERR_SNIPPET:]} {snippet}") from None
        if data.get("is_error"):
            raise RuntimeError(f"claude CLI reported an error: {data.get('result') or data.get('subtype')} {snippet}")
        return to_result(data, model)

    async def _run(self, argv: list[str], stdin: str) -> tuple[str, str, int]:
        """Fresh temp cwd: the CLI reads no CLAUDE.md and can't write into any repo."""
        timeout = self.settings.claude_code_timeout
        with tempfile.TemporaryDirectory(prefix="al-claude-code-") as cwd:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv, cwd=cwd, env=child_env(), stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            except FileNotFoundError:
                raise RuntimeError(f"claude CLI not found: {argv[0]} (set CLAUDE_CODE_BIN)") from None
            try:
                out, err = await asyncio.wait_for(proc.communicate(stdin.encode()), timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise TimeoutError(f"claude CLI did not answer within {timeout}s (CLAUDE_CODE_TIMEOUT)") from None
        return out.decode(errors="replace"), err.decode(errors="replace"), proc.returncode or 0

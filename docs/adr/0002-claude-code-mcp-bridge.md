# ADR-0002: Landscape tools for `claude-code/*` agents via a loopback MCP bridge

- **Status:** Accepted (verified against real CLI 2.1.311)
- **Date:** 2026-11-06
- **Deciders:** human owner, integrator
- **Builds on:** [ADR-0001](0001-claude-code-local-provider.md)

## Context
- ADR-0001 made `claude-code/*` a local, opt-in provider driving `claude -p` – **text only**: built-in tools off,
  no MCP, so agents on it could not read or edit repos, files, knowledge bases or call MCP tools.
- In practice this produced missions with editable repos but **zero commits** (all agents on `claude-code/*`).
- Our tool loop (`Agent.think`) expects OpenAI `tool_calls`. `claude -p` never returns those; it runs its own loop.
- Hard rules: sandboxed paths, protected secrets, per-profile tool families, `MAX_TOOL_STEPS`, every call visible
  (🔧 events, report), agents never push, no shell.

## Options considered
| Option | Summary | Verdict |
|---|---|---|
| **A. MCP bridge** | Expose the agent's toolbox as an MCP server on our own app; the CLI gets only that server | **Chosen.** All checks stay in `Toolbox.call` |
| B. Text protocol | Model writes tool calls as JSON text; we parse and start a new `claude -p` per step | Rejected: fragile, 1–3 s + cache tokens per step |
| C. Built-in tools in the worktree | cwd = worktree, allow Read/Edit/Grep | Rejected: bypasses sandbox, secret protection, logging and the no-shell rule |

## Decision
- Per agent turn with tools, `Agent.think` opens a **bridge session** (`context/mcp_bridge.py`): random token,
  tool specs = `toolbox.specs(agent)` (re-read on every `tools/list`), executor = `Agent._run_tool` with the task's
  usage record carried over (the CLI calls back on another asyncio task). The session closes when the turn ends.
- The app serves MCP streamable HTTP in its simplest form at `POST /mcp/bridge/{token}` (JSON-RPC → JSON, no SSE):
  `initialize`, `tools/list`, `tools/call`, `ping`. Loopback clients only; unknown token → 404.
- The runner adds `--allowedTools mcp__landscape --permission-mode dontAsk` and
  `--strict-mcp-config --mcp-config '{"mcpServers":{"landscape":{"type":"http","url":…}}}'`; `--tools ""` stays.
- The step budget is enforced in the bridge (`max_steps` → "Tool budget exhausted"); unknown tool names are refused.
- `CLAUDE_CODE_TOOLS` (default on) switches the bridge off (text-only behaviour of ADR-0001).
- With the bridge on, the "repo editors must not run on claude-code/*" guard and the briefing warning are skipped.

## Consequences
- ✅ `claude-code/*` agents can research and change repos, search knowledge, run checks – same rules as gateway models.
- ✅ Tool calls appear in the map and the report exactly like gateway tool calls.
- ⚠️ Claude Code runs the loop: the whole loop must finish within `CLAUDE_CODE_TIMEOUT` (300 s default).
- ⚠️ Mid-turn token usage is reported once per CLI run (no per-step accounting).
- ⚠️ CLI flags (`--allowedTools`, `--permission-mode dontAsk`, HTTP MCP) are a moving target – re-verify on upgrades.

## Verification log
| Date | CLI | Check | Result |
|---|---|---|---|
| 2026-11-06 | 2.1.311, macOS, haiku | bridge tool `lookup_code` offered, prompt asks for a code | ✅ 1 call `lookup_code({"name":"alpha"})`, answer `ZEBRA-19` |
| 2026-11-06 | same | prompt asks to run Bash and read `/etc/hosts` | ✅ refused: "only tool I have is `mcp__landscape__lookup_code`" |
| 2026-11-06 | fake CLI (tests) | full mission, folder attached, agent on `claude-code/sonnet` | ✅ `read_file` via bridge, sandboxed result in report, sessions closed |

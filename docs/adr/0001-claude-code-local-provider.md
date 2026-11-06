# ADR-0001: Run Claude Code as a local, opt-in model provider – not on the LiteLLM gateway

- **Status:** Accepted (prototype, verified against real CLI 2.1.311)
- **Date:** 2026-11-06
- **Deciders:** human owner, Architect Kilo-2 (draft), Orchestrator India-2, integrator

## Context
- The user has a **Claude Code Enterprise seat** (company subscription) and **no Anthropic API key**, and does not
  want one. LiteLLM is already the model gateway for all other models.
- LiteLLM's Anthropic routes need an API key, Bedrock, Vertex or Azure credentials. A subscription seat is none of these.
- **Paperclip `claude_local`** solves this by spawning the official `claude` CLI locally under the user's own login and
  reading its output. This is the pattern we follow.
- **pi.dev `pi-claude-code-provider`** describes a similar provider. We have **not verified** how it works, so we do not rely on it.
- Sources: mission archive *"At the moment i have LiteLLM running as a Mo…"* (round 1, `al-platform-lifecycle-knowledge`),
  LiteLLM integration research (`al-litellm-agent-integration-know`).

## Options considered
| Option | Summary | Pros | Cons |
|---|---|---|---|
| A | LiteLLM `CustomLLM` on the shared gateway that drives the CLI | One place for all models, spend logging | **Rejected:** shares one personal seat with every gateway user; puts a personal credential on gateway infrastructure |
| B | Local opt-in provider `claude-code/*` inside agent-landscape that drives the `claude` CLI | Personal login stays on the user's machine; off by default; easy to fake in tests | **Chosen.** No landscape tools yet; one process per call |
| C | Claude Code as an A2A ally (wrapped as a remote agent) | Fits the existing ally model; could keep Claude Code's own tools | **Deferred:** needs a separate A2A wrapper and hosting; revisit with the MCP bridge |
| D | Extract the OAuth token / use CLIProxyAPI-style "Max-plan proxies" | Looks like a normal API | **Rejected:** against Anthropic's terms and actively blocked |

## Decision
**LiteLLM hosts only official routes** (API keys, Bedrock, Vertex, Azure). `claude-code/*` is a **local, opt-in,
loopback-only** provider inside agent-landscape. It drives the official `claude` CLI under the user's own login
(option B). It is off unless `CLAUDE_CODE_ENABLED=1`. It refuses calls when `HOST` is not loopback unless
`CLAUDE_CODE_ALLOW_REMOTE=1`. It runs the CLI with built-in tools and MCP disabled, in an empty temp dir, with
`ANTHROPIC_API_KEY` stripped from the child env. This keeps the personal seat personal (rules out A), stays within the
official client (rules out D) and can ship now without new hosting (C later).

## Consequences
- ✅ Subscription users can put Claude on selected agents without an API key and without touching the shared gateway.
- ✅ Mock mode never spawns the CLI, so tests and demos stay offline.
- ⚠️ **No landscape tools** on `claude-code/*` agents in slice 1 (text only). Use them for synthesis, orchestration or writing roles.
- ⚠️ **Cost is shown as $0** (subscription). The CLI's notional cost is kept separately for reference.
- ⚠️ **The seat's quota is shared** by every agent that uses it. Large swarms can exhaust it.
- ⚠️ **Each call spawns a process.** Latency is not yet measured.
- ⚠️ **CLI flags are a moving target.** Pin and re-check the `claude` version when it changes.
- ✅ **Policy resolved (2026-11-06):** the company approved headless use of the Enterprise seat by a local tool
  (`claude -p`) for local, personal use. Shared/hosted use stays out of scope, so the loopback guard stays.
- 🔜 MCP bridge for landscape tools (own ADR), streaming (`stream-json`), UI badge "subscription · notional $", revisit option C.
- ✖️ Session reuse (`--resume`) dropped: the 1-hour prompt cache gives most of the benefit without state on disk.

## Update 2026-11-06 – owner decisions after the real smoke test
1. **Token rule:** for `claude-code/*`, `tokens` = **input + output only**. Cache read/write tokens are kept as
   `cache_read_tokens` / `cache_write_tokens`, shown separately and **not** added to totals or XP. The LiteLLM path is
   unchanged, so counts are not directly comparable with gateway models.
2. **User `~/.claude` is blocked:** the runner always passes `--setting-sources project` (cwd = empty temp dir), so
   user CLAUDE.md/memory and `settings.json` (`env`, `apiKeyHelper`, `hooks`) are not loaded; managed policy applies.
   Rejected alternatives: `--bare` (API-key auth only), overriding `HOME` / `CLAUDE_CONFIG_DIR` (loses the seat login).
3. **Hardening:** strip `ANTHROPIC_*` / `CLAUDE_CODE_USE_*` by prefix, `DISABLE_AUTOUPDATER=1`, `--no-session-persistence`,
   process-group kill on timeout and cancel, error JSON preferred over exit codes (`is_error`, `api_error_status`),
   oversized system prompts moved to stdin, agents on `claude-code/*` are told they have no tools.

## Verification log
| Date | CLI / OS | Check | Result |
|---|---|---|---|
| 2026-11-06 | 2.1.311, macOS | `claude --help` | ✅ `--tools`, `--system-prompt`, `--setting-sources`, `--no-session-persistence`, `--strict-mcp-config`, `--mcp-config` (variadic) exist |
| 2026-11-06 | same | owner run, default prompt, `sonnet` | ✅ JSON shape (`result`, `usage`, `total_cost_usd`, `is_error`, `subtype`, `api_error_status`, `modelUsage`) → `tests/fixtures/claude_p_result.json`; `sonnet` → `claude-sonnet-5-5` (firstParty) |
| 2026-11-06 | same | `python -m agent_landscape.claude_code haiku` (runner argv) | ✅ `PONG`, 7 tokens, cache write 594 (vs 1873 with the default prompt → `--system-prompt` replaces it), ~1.2 s, `claude-haiku-5-5`, no session folder left |
| 2026-11-06 | same | init event (`stream-json --verbose`) with runner flags | ✅ `tools: []`, `mcp_servers: []`, `apiKeySource: none` (seat login) |

---
name: agent-landscape-maintenance
description: Conventions, commands and report formats for maintaining the agent-landscape repo itself – use for dead-code / test-coverage reviews, implementing tests or features in agent-landscape, and judging feature maturity.
---
# Maintaining agent-landscape

## Repo map
- `src/agent_landscape/` – the package
  - `agents/` – agent classes (`base.py` LLM + tool loop, `units.py` Worker/Orchestrator/Commander/Swarm, `ops.py`),
    `profiles.py` + built-in `profiles/*.md`, `roles.py` (prompt builders)
  - `context/` – mission context: `intel.py`, `toolbox.py` (tool specs + dispatch), `sandbox.py`, `repos.py`,
    `checks.py`, `vectorstore.py`, `mcp.py`, `skills.py`, `documents.py`
  - `runtime/` – `runtime.py` (topics, agents, events), `persistence.py`, `regions.py`, `hexgrid.py`, `reports.py`
  - `a2a/` – A2A v0.3 JSON-RPC server/client · `storage/db.py` – the only module that speaks SQL
  - `web/app.py` (FastAPI) + `web/static/*.js` (vanilla JS, no build step)
- `tests/` – pytest, `asyncio_mode = auto`; e2e tests start a real uvicorn server in a thread
- `profiles/`, `skills/`, `repos.yaml` – user configuration versioned with the repo

## Hard rules (breaking them causes real bugs)
- `runtime/__init__.py` stays import-free (import cycle agents ↔ runtime).
- E2E tests drive the server **only via HTTP** – never call runtime coroutines from the test's event loop.
- Mock mode (`MOCK_LLM=1`, `Settings(mock=True)`) must never call external services (LLM, MCP, vector store, git push).
- Agents never push; only humans push/merge. Agents get no shell – only `run_check`.
- Keep changes minimal and in the existing style: type hints, `from __future__ import annotations`, dataclasses,
  small functions, short docstrings explaining *why*. No new dependencies without saying so in the deliverable.

## Checks (`run_check`)
| check | use it for |
|---|---|
| `tests` | full suite, or `target: tests/test_x.py`, `keyword: "repo and not e2e"` |
| `coverage` | % per file + missing lines (`term-missing`) |
| `lint` | ruff – unused imports/variables, obvious bugs |
| `dead_code` | vulture candidates (≥ 60 % confidence) – **verify every hit** with `search_files` |

vulture false positives here: FastAPI route functions, pytest fixtures, dataclass fields read via `asdict`,
methods called from JS through the API, `__main__` entry points, names used only in f-strings / `getattr`,
pydantic / A2A model fields in `a2a/types.py` (wire format – serialised, never read in Python),
re-exports in `__init__.py` (public API).

## Finding format (janitor + scout)
Report every finding as its own block so it can be turned into an approvable proposal:

```
### F<n> · <short imperative title>
- kind: dead-code | redundant-test | missing-test | flaky-test | bug | refactor
- where: path/to/file.py:<line>[-<line>] (symbol)
- evidence: <check output line or search result that proves it>
- action: <exactly what to change>
- value: <why it is worth it – risk reduced / lines removed / behaviour now protected>
- risk: low | medium | high · effort: S | M | L · confidence: <0-100>%
```
Order findings by value ÷ effort. Max 8 per run – fewer good ones beat many weak ones. Say explicitly when
nothing is worth doing.

## Maturity scale (feature-steward)
| level | meaning |
|---|---|
| 0 idea | spec only |
| 1 prototype | happy path works in mock mode, no tests |
| 2 usable | works with real services, tests for the main path, documented in README / features.log |
| 3 solid | edge cases + failure modes handled and tested, persisted across restarts, UI polish |
| 4 hardened | used for real, no open issues, coverage of the new code ≥ 80 % |

# Agent profiles

Each `*.md` file here configures one agent type: frontmatter for settings, body for the **system instructions**.
Files override the built-in profiles in `src/agent_landscape/agents/profiles/` **by key**, or add new ones.
Edit them in the UI (🧬 Agents tab) or directly in VS Code. Changes apply on the agent's next LLM turn.

```markdown
---
key: architect                 # file name = key, lowercase-with-dashes
kind: worker                   # worker | orchestrator | swarm_leader | swarm_member
title: Architect
icon: 🏛️
description: Orchestrators pick specialists by this line – keep it specific.
model: bc2_claude-haiku-5-5    # optional – mission model > profile model > defaults
temperature: 0.4               # optional
max_tool_steps: 12             # optional – tool-call rounds per answer (default MAX_TOOL_STEPS=6, max 40)
tools: [intel, files, knowledge, skills]   # tool families (see below)
grant_mcp: [tavily_search_mcp] # optional – LiteLLM MCP servers (read-only tools) or "server/tool"
skills: [architecture-decision-record]     # ["*"] = all repo skills, [] = none
prompts:                       # orchestrator / swarm_leader only
  plan: |                      # {max_subtasks} placeholder
    ...
  synthesize: |
    ...
enabled: true
---
You are a pragmatic software architect …
```

**Tool families**

| Family | Tools |
|---|---|
| `intel` | Read the documents attached to the mission. |
| `files` | List, read and search attached folders (read-only). |
| `knowledge` | Search the region and mission knowledge bases (vector stores). |
| `knowledge_admin` | Create stores and file documents into them (orchestrators). |
| `mcp` | MCP tools attached to the mission. |
| `skills` | Repo skills via `use_skill` / `read_skill_file`. |
| `repo` | Inspect attached git repos: `repo_info`, `repo_log`, `repo_diff`. |
| `repo_write` | ✍️ Edit files in **editable** repos and commit to the mission branch (`edit_file`, `write_file`, `delete_file`, `repo_commit`). It never pushes. Orchestrators show these specialists as "✍️ can edit repos". |
| `checks` | 🧪 `run_check` in a mission repo's worktree: `tests` (pytest), `coverage` (pytest-cov), `lint` (ruff), `dead_code` (vulture). It uses a fixed allow-list and gives agents no shell. Secrets are blanked and `MOCK_LLM=1` is set, but the tests still execute repo code. Needs `uv pip install -e '.[checks]'`. |

**Lifecycle team** (maintains this repo): `code-janitor` 🧹, `coverage-scout` 🔬, `integrator` 🔩, `feature-steward` 🧭 (lead). They share the `agent-landscape-maintenance` skill.

Every tool call is checked against the agent's profile.

**Skills** are folders named `skills/<name>/` containing a `SKILL.md`. They are also found in `.github/skills/` and `.claude/skills/`. Each `SKILL.md` has frontmatter `name` + `description`; the body holds the instructions. Agents see only the descriptions and load a skill on demand.

To reset a built-in profile, delete its override file here, or use the **↩️ Reset** button in the UI.

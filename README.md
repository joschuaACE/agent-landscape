# ⬢ Agent Landscape

Agent Landscape orchestrates agents, sub-agents and swarms. Every unit speaks the
**A2A (Agent-to-Agent) protocol**, and models are called through **LiteLLM**. A live **hex strategy
map** shows which agents are running, which mission (topic) they work on, and what they are doing right now.

- **A2A-native.** Each agent, including dynamically spawned sub-agents and swarm drones, gets its own
  A2A JSON-RPC endpoint and agent card. Parents delegate to children over A2A (`message/stream`),
  and cancellation propagates down the whole tree (`tasks/cancel`).
- **LiteLLM-compatible on both sides.**
  - *Models:* any LiteLLM model string, called directly or routed through a LiteLLM Proxy.
  - *Agents:* endpoint paths and the 0.3 wire format match LiteLLM's Agent Gateway, so the Commander can be
    registered there, and agents hosted in LiteLLM can join as allies.
- **Gamified UI.**
  - Each mission claims a hexagon territory. Agents are units on its tiles.
  - A2A messages fly between units as projectiles.
  - Units earn XP and levels, and you can unlock achievements and get promoted.

## Quickstart

```bash
uv venv && uv pip install -e ".[dev]"
cp .env.example .env            # configure models / LiteLLM proxy (optional)
.venv/bin/agent-landscape --mock  # no tokens spent (uses LiteLLM's mock_response)
# or live:
.venv/bin/agent-landscape
```

Open http://127.0.0.1:8765, press **N** (new mission briefing), then **⚔️ Deploy agents**.

## Architecture

```mermaid
flowchart LR
  UI[Hex map UI] -- WebSocket events --> RT
  UI -- REST /api/missions --> RT[Runtime]
  EXT[A2A client / LiteLLM Agent Gateway] -- JSON-RPC --> CMD
  RT -- A2A message/stream --> CMD[👑 Commander]
  CMD -- A2A --> ORC[🏰 Orchestrator]
  ORC -- A2A --> W1[🔭 Specialist]
  ORC -- A2A --> CAP[🏰 Captain = sub-orchestrator]
  ORC -- A2A --> HIVE[🌀 Hive leader]
  HIVE -- A2A --> D1[🐝 Drone] & D2[🐝 Drone] & D3[🐝 Drone]
  ORC -- A2A --> ALLY[🤝 Remote A2A ally]
  W1 & D1 & ORC -. litellm.acompletion .-> LLM[(LiteLLM / Proxy)]
```

| Unit | Role |
|---|---|
| 👑 Commander | Persistent entry point (`/a2a/commander`). Each message becomes a mission with its own territory. |
| 🏰 Orchestrator / Captain | Plans (LLM → JSON), spawns children, delegates in parallel, synthesizes. Captains are nested orchestrators. |
| 🔭🛠️✒️📊🛡️🎨 Specialists | researcher, engineer, writer, analyst, critic, designer |
| 🌀 Hive leader + 🐝 drones | Swarm: N agents with different personas answer in parallel; the leader builds the consensus. |
| 🤝 Ally | External A2A agent (e.g. hosted in LiteLLM), proxied and usable by orchestrators. |

**Formations** (mission `mode`): `auto` (the orchestrator picks single/swarm/delegate per subtask),
`hierarchy` (no swarms) and `swarm` (the whole mission is a single swarm).

### Topic regions (charted by the Command HQ)

Before deploying a mission, the Commander **surveys** it (one LLM call). It either groups the mission into an existing
**region** (same product, project or domain) or charts a new region with a fitting **biome**. Each region holds up to
19 mission territories and renders with its own terrain and decorations. Unexplored land stays under fog.

| Biome | Typical topics |
|---|---|
| 🌻 Sunflower Fields | greenfield concepts, new platforms, ideation |
| 🏜️ Desert | vendor / low-code platforms (Copilot Studio, Power Platform, SharePoint …) |
| 🔮 Crystal Caves | LLM / ML engineering |
| 🏙️ Metropolis | infrastructure, backend, DevOps |
| 🌲 Deep Forest | research and analysis |
| 🌋 Volcano | incidents, security, risk |
| ❄️ Frozen Tundra | compliance, legal, governance |
| 🐸 Swamp | legacy and tech debt |
| 🌴 Tropical Isles | marketing, sales, launches |
| 🍁 Autumn Woods | people, communication, mails |
| ⛰️ Highlands | architecture, big decisions |

You can override the HQ in three ways: pick a region in the launch form, change a region's biome in the sidebar, or rename it
(`PATCH /api/regions/{id}`). Over A2A, pass `metadata.region_id`.

### Reuse, memory and feedback cycles

- **Service record:** every unit remembers its last 3 assignments and results, and this memory goes into its prompts.
  Units that have served ≥ 2 tasks are 🎖️ veterans.
- **Feedback rounds:** send feedback on a finished mission (💬 in the sidebar, `POST /api/topics/{id}/feedback`, or simply
  another A2A message in the mission's `contextId`). The **same orchestrator and team** re-work the deliverable with their
  memory. Nothing new is spawned unless the revised plan needs it.
- **Veterans across missions:** new missions in a region first recruit idle units of that region: own former team first,
  then the region, most experienced first. Recruited units march over to the new territory. Fresh units are spawned only when no veteran fits.

## A2A protocol

The implementation follows the A2A spec v0.3 (JSON-RPC 2.0, objects discriminated by `kind`). It uses the same paths as LiteLLM:

| Endpoint | Purpose |
|---|---|
| `POST /a2a/{agent_id}` | JSON-RPC: `message/send`, `message/stream` (SSE), `tasks/get`, `tasks/list`, `tasks/cancel`, `tasks/resubscribe`, `agent/getAuthenticatedExtendedCard` |
| `POST /a2a/{agent_id}/message/send` | alias for `message/send` |
| `GET /a2a/{agent_id}/.well-known/agent-card.json` | agent card (also `agent.json`) |
| `GET /.well-known/agent-card.json` | the Commander's card |

```bash
curl -s localhost:8765/a2a/commander -H 'Content-Type: application/json' -d '{
  "jsonrpc": "2.0", "id": 1, "method": "message/send",
  "params": {"message": {"kind": "message", "role": "user", "messageId": "m1",
    "parts": [{"kind": "text", "text": "Compare vector DBs for our RAG stack"}],
    "metadata": {"mode": "auto", "swarm_size": 4, "title": "RAG stack"}}}}'
```

Optional message `metadata` keys: `mode`, `swarm_size`, `title`, `model` (sets the LiteLLM model for the mission),
`subtask` (short objective, used by parents). Incoming `X-LiteLLM-*` headers (trace id, agent id) are forwarded
to every LLM call made while serving the request, which keeps LiteLLM's trace grouping and per-agent spend attribution intact.

## LiteLLM integration

**Use LiteLLM models.** Set `DEFAULT_MODEL` (and optionally `ORCHESTRATOR_MODEL`) to any LiteLLM model string. Then use either:
- **Provider keys:** set e.g. `OPENAI_API_KEY` and models are called directly via the LiteLLM SDK.
- **Proxy (recommended):** set `LITELLM_PROXY_URL` and `LITELLM_PROXY_API_KEY`. Models are then called as
  `litellm_proxy/<model>`, and the UI's model picker lists the proxy's `/v1/models`.

> **Use the public model name.** The proxy routes by the name you gave the model in LiteLLM (`model_name`,
> e.g. `bc2_claude-haiku-5-5`), not by the underlying provider model shown under it (`litellm_params.model`,
> e.g. `claude-haiku-5-5`), which returns a 403 "not available for this API key". At startup the server checks
> `DEFAULT_MODEL`/`ORCHESTRATOR_MODEL` against the proxy. If the name is wrong, it logs the correct public name and the UI badge turns red.

Models that reject `temperature` (e.g. reasoning models behind a proxy) are detected automatically and retried without it.

**Manage the landscape from LiteLLM.** Register the Commander in LiteLLM's Agent Gateway (see `litellm_config.example.yaml`,
or the **🤝 Allies** tab for a ready-to-copy snippet):

```yaml
agents:
  - agent_name: agent-landscape
    agent_card_params:
      name: "Agent Landscape Commander"
      url: "http://127.0.0.1:8765/a2a/commander"
      protocolVersion: "0.3"
```

**Use LiteLLM-hosted agents.** In **🤝 Allies**, recruit `http://localhost:4000/a2a/<agent>` with your LiteLLM key.
Orchestrators can then assign subtasks to it (`"strategy": "ally"`).

### Claude Code (local, opt-in – prototype)

**For subscription users without an API key.** If you have a Claude Code seat (e.g. Enterprise) but no Anthropic API key,
agents can use Claude through the official `claude` CLI under your own login. LiteLLM keeps only the official routes
(keys, Bedrock, Vertex, Azure); this provider runs locally inside Agent Landscape.

- **Setup:** install the `claude` CLI and log in once (`claude` → `/login`). Set `CLAUDE_CODE_ENABLED=1` and pick models
  such as `claude-code/sonnet` (mission model, profile model or `DEFAULT_MODEL`).
  Prefer an absolute `CLAUDE_CODE_BIN` if you start the server from an IDE, launchd or a conda env.
- **Local only:** `claude-code/*` calls are refused when `HOST` is not loopback, unless `CLAUDE_CODE_ALLOW_REMOTE=1`.
  Never expose it as a shared service – it is your personal seat. Start the server from a Terminal in your logged-in
  macOS session (over ssh/launchd the Keychain login may be unavailable).
- **Locked down:** built-in tools and MCP are disabled, the CLI runs in an empty temp dir without session persistence,
  all `ANTHROPIC_*` / `CLAUDE_CODE_USE_*` variables are stripped from its env and auto-update is off for the child.
  A cancelled or timed-out call kills the CLI's whole process group.
- **Your personal Claude Code setup is ignored:** the runner always passes `--setting-sources project`, so
  `~/.claude/CLAUDE.md` and `~/.claude/settings.json` (`env`, `apiKeyHelper`, `hooks`) are **not** loaded. Your
  company's managed policy still applies.
- **Landscape tools via MCP bridge:** `claude -p` runs its own tool loop, so it gets exactly one MCP server: a
  loopback endpoint of this app (`/mcp/bridge/<token>`) serving *that agent's* toolbox – repo, files, knowledge,
  skills, checks, LiteLLM MCP tools. Every call still goes through the toolbox (profile tool access, sandboxes,
  secret protection, 🔧 events in the map and report, `MAX_TOOL_STEPS` budget). Claude Code's own tools (Bash,
  Edit, Read …) stay off; only `mcp__landscape__*` is allowed (`--permission-mode dontAsk`). One random token per
  agent turn, loopback clients only. `CLAUDE_CODE_TOOLS=0` switches back to text-only. See
  [ADR-0002](docs/adr/0002-claude-code-mcp-bridge.md).
- **Limitations:** no streaming, the seat's quota is shared by every agent using it, and a whole tool loop must
  finish within `CLAUDE_CODE_TIMEOUT`.
- **Tokens & cost:** `tokens` = input + output only. Prompt-cache traffic is shown separately in reports
  (`cache … read / … write`) and never counted into totals or XP. Cost is $0 (subscription); the CLI's would-be API
  price is kept as `notional_cost`. Token counts are therefore not directly comparable with gateway models.
- **Approved** for local, personal headless use of the Enterprise seat (2026-11-06). Shared/hosted use is out of scope.

**Verify your setup** (one cheap `haiku` call; expect `RESULT: OK`):

```bash
HOST=127.0.0.1 python -m agent_landscape.claude_code        # prints argv, text, tokens, served model
CLAUDE_CODE_LIVE=1 pytest tests/test_claude_code.py -k live  # same check as a test
```

Why this lives here and not in LiteLLM: see [ADR-0001](docs/adr/0001-claude-code-local-provider.md).

## Mission briefing: context & tools

Press **N** (or **⚔️ New mission briefing**) to open the briefing composer. The objective stays in focus. Below it, a
compact **Context** list shows what the agents may use: one row per item, with tools grouped per MCP server. Click a row
to change it, or **✕** to remove it.

Everything is added in one **＋ Add context** dialog. Type **`#`** in the objective to open it on a category, or drop
or paste files straight onto the objective.

- **Categories:** ✨ Suggested · 📦 Repositories · 📚 Knowledge · 🔌 Tools · 📁 Files & folders · 🔗 Web pages. One search box
  covers repositories, knowledge and tools.
- **Suggested** lists matches for your title and objective: repo names and keywords, MCP servers you mention, and
  knowledge bases with matching terms. A 💡 hint under the context list points to them.
- **Settings appear only when needed.** Ticking a repository reveals *👁 Read-only / ✍️ Edit on a mission branch* and the
  base branch. A tool server expands to its individual tools; tools that change data are flagged.
- **Draft selection.** Nothing changes until you press **Apply** (or **⌘↵**). **Cancel** or **Esc** discards the draft.
- **Registering a repository** is its own screen inside the dialog, so the briefing itself never shows a setup form.

| Context | What agents get |
|---|---|
| 📦 **Repository** | An isolated git worktree. See *Git repositories* below. |
| 📚 **Knowledge** | `kb_search` in the linked vector stores. See *Knowledge bases* below. |
| 📎 **Files**: text / code / markdown (≤ 1 MB) and **PDF, Word, PowerPoint, Excel** (≤ 25 MB) | The document. Office files and PDFs are converted to Markdown locally with [markitdown](https://github.com/microsoft/markitdown), also when they arrive as A2A `FilePart`s. Scanned PDFs without a text layer are not supported (no OCR). Small documents are inlined into every agent's briefing; larger ones are read on demand (`read_intel`). |
| 📁 **Folder / path** | Read-only tools `list_dir`, `read_file`, `search_files`, sandboxed to that folder. `.env`, keys, `.git`, `node_modules` … are hidden. Paths must be inside `CONTEXT_ROOTS` (default: your home dir). |
| 🔗 **URL** | The page is fetched at deploy time and attached as a document. |
| 🔌 **Tools** | MCP tools from the **LiteLLM MCP gateway** (`/mcp-rest`). Selecting a server attaches its read-only tools. Tools that change data (⚠️) must be picked one by one. |

Agents run a tool-calling loop (max `MAX_TOOL_STEPS`, default 6). Every call shows up as a 🔧 bubble on the map and is listed
per artifact in the mission report. In mock mode, local tools really run and external MCP tools are simulated.

Over A2A, context is sent the A2A-native way: text files as `FilePart`s, references as a `DataPart`
`{"type": "agent-landscape/context", "items": [{"kind": "path", "path": "…"}, {"kind": "tool", "tool": "tavily_search_mcp"}]}`.
This means LiteLLM's Agent Gateway or any A2A client can attach context too.

**Other REST APIs:** register them in LiteLLM as an MCP server **from their OpenAPI spec**. They then appear in the
🔌 picker with no code in this project, and access is managed per key/team in LiteLLM. (The vector store has a native
integration instead, see *Knowledge bases* below; this snippet is only useful if *other* clients such as VS Code should search it.)

```yaml
mcp_servers:
  vector_store:
    url: "http://localhost:8000"
    spec_path: "http://localhost:8000/openapi.json"
    auth_type: bearer_token
    auth_value: os.environ/VECTOR_STORE_KEY
    allowed_tools: ["search_vector_store_route_vector_store__vector_store_id__search_post"]  # optional allowlist
litellm_settings:
  user_url_allowed_hosts: ["localhost:8000"]   # spec fetch from localhost is SSRF-blocked by default
```

## Knowledge bases (vector stores)

With `VECTOR_STORE_URL` / `VECTOR_STORE_KEY` set, agents get **knowledge-base tools** (native REST integration):

| Tool | Who | What |
|---|---|---|
| `kb_search` | every agent of the mission | Semantic search, but **only in bases linked to its mission** |
| `kb_catalog` → `kb_link` | orchestrators | Find an existing base and link it to the mission |
| `kb_create` | orchestrators | New base named **`al-<region>-<purpose>`** (unique, deduplicated), optionally seeded with the attached documents |
| `kb_add` / `kb_ingest` | orchestrators | Add findings, or index text files from attached folders (sandboxed, secrets hidden) |

- **Provenance:** each store's metadata records `owner: agent-landscape`, purpose, description, region, mission and creating agent.
  Stores created outside Agent Landscape are **read-only** for agents.
- **Region memory:** completed missions are archived into **`al-<region>-knowledge`**, as the **full mission report**: objective, intel,
  each agent's input/output and tool calls. Every new mission in that region gets this store linked automatically, so teams build on earlier work.
- **Recall:** before choosing a region for a new mission, Command HQ runs a semantic search over **all** region archives
  (`VECTOR_STORE_RECALL`, top 5 by default; `0` turns it off).
  - The hits guide which region the mission goes to.
  - The archives they come from are linked to the mission (🧠 *recalled*), including archives from other regions.
  - The orchestrator's order gets a short *Recall: related past missions* briefing.
- **Mission archive import:** on startup, exported reports in `mission-archive/**/*.md` (`MISSION_ARCHIVE_DIR`, `off` disables it) are
  imported into the region knowledge base named in their `Region:` line. Files already imported, with the same path and content, are skipped.
  Re-run the import with `POST /api/knowledge/import-archive`.
- **UI:** link bases in the briefing (📚 Knowledge). The report shows which bases a mission used, who created them and which agents searched them.
  `GET /api/knowledge` lists all bases.
- Mock mode uses an in-memory store and never touches the real service.

**Why native and not MCP?** Creating, naming, linking and permissions all need mission context (region, mission, agent role)
that only Agent Landscape has. An OpenAPI-generated MCP server would expose raw operations (incl. delete, without the naming
convention or per-role limits) to every agent. If other clients (VS Code, Cursor) should search the same bases, add the
OpenAPI MCP server in LiteLLM *in addition*. Better still, build a small curated MCP server with `search`/`list` only.

## Agent profiles & skills

Every agent type (orchestrator, each specialist, hive leader, each swarm persona) is a **profile**: a Markdown file with its
**system instructions**, prompt templates, model, temperature, **tool access** and allowed **skills**.

- **Built-ins:** `src/agent_landscape/agents/profiles/*.md` (formerly `roles.py`).
- **Your profiles:** `profiles/*.md` in this repo (`PROFILES_DIR`). A file with the same `key` overrides a built-in, a new key adds
  an agent type. New specialists are offered to orchestrators automatically, chosen by their `description`. Versioned in git.
- **UI:** the **🧬 Agents** tab lists all profiles and skills. Click one to edit it. *Save* writes `profiles/<key>.md`, *Reset* removes
  the override, *Duplicate* creates a variant. Changes apply on an agent's next LLM turn, even for veterans already on the map.
- **Per mission:** the briefing's *Lead profile* picks the root orchestrator or hive leader. Sub-orchestrators inherit it.
- **Tool access** by family (`intel`, `files`, `knowledge`, `knowledge_admin`, `mcp`, `skills`), plus `grant_mcp` for always-on
  LiteLLM MCP servers (read-only tools). It is enforced on every call, not only hidden from the tool list.
- **Model precedence:** mission model > profile model > `ORCHESTRATOR_MODEL` (orchestrators) > `DEFAULT_MODEL`.
- **Model choice per mission** (briefing → *MODEL*):
  - **default**: each profile's model, else `DEFAULT_MODEL`.
  - **a model**: forced for every agent of the mission.
  - **🧠 Auto**: tick a **pool** of models. Orchestrators (and captains) assign a `model` per subtask by complexity,
    e.g. a small model for lookups and summaries, a strong one for architecture, code and reviews. Picks outside the
    pool are ignored. `claude-code/*` models are unticked by default and marked *no tools*. The assignment is logged,
    and every artifact in the report shows the model it ran on. Over A2A: `metadata.model = "auto"` and optionally
    `metadata.model_pool = [...]` (default: all offered models).
  - A model pinned on a unit (Unit panel) always wins.
- **Traceability:** each A2A task records `profile`, `profile_rev` (content hash) and `skills_used`. The report's 🧩 Artifacts tab shows
  which instructions produced which output.

**Skills** follow the Anthropic / VS Code `SKILL.md` format: a folder `skills/<name>/` with `SKILL.md` (frontmatter `name` and
`description`, body = instructions) and optional extra files. They are also discovered in `.github/skills/` and `.claude/skills/`
(`SKILLS_DIRS`). Agents see only name and description in their briefing. They load the instructions with `use_skill` and extra
files with `read_skill_file`, which keeps prompts small. Example: `skills/architecture-decision-record/`, used by
`profiles/architect.md`.

> **LiteLLM skills:** the proxy's `/v1/skills` registry only stores metadata for Anthropic's code-execution containers. It
> exposes no instructions an agent could read, so these agents cannot use those skills. Keep skills in the repo. A sync that
> uploads repo skills to LiteLLM for other clients is possible later.

See `profiles/README.md` for the file format.

## Git repositories (code missions)

You can register repos and attach them to a mission, e.g. a Copilot Studio solution repo for "add a topic" or "update a workflow".

- **Register** in the briefing with **＋ Add context → 📦 Repositories → ➕ Register repository**, or in `repos.yaml` (`REPOS_FILE`, versioned). A repo has:
  - a **remote URL**, cloned into `workspace/repos/<name>` with *your* git credentials (credential helper / SSH agent; nothing is stored), or a **local checkout** used in place;
  - an optional base branch;
  - a description, which agents read;
  - keywords. The composer shows 💡 suggestions when your briefing mentions a repo's name or keywords.
- **Per mission** each repo gets an **isolated `git worktree`** under `workspace/missions/<mission>/<repo>`. Your working copy is never touched. You choose the mode:
  - **👁 Read**: a detached snapshot. The orchestrator and specialists research it with `list_dir`, `read_file`, `search_files`, `repo_info`, `repo_log` and `repo_diff`.
  - **✍️ Edit**: a fresh branch `agents/<mission>-<id>`. Specialists whose profile has the `repo_write` family (built-in: 🛠️ Engineer) change files with `edit_file`, `write_file` or `delete_file` and commit with `repo_commit`.
    - Paths are confined to the worktree; `.git` and secrets are protected.
    - Commits are authored as `<Agent> (Agent Landscape)`.
- **Planning:** the orchestrator researches the repo first, then plans with concrete file paths. It hands implementation to ✍️ specialists and reviews with `repo_diff` before it synthesizes.
- **Review & ship:** the report's **📦 Code** tab shows the branch, commits and the colored diff, with these actions:
  - **🧩 Open in VS Code**: opens the worktree, so you can amend, test or run `pac`.
  - **⬆️ Push branch**: a human-only action. Agents never push.
  - **🔀 Create PR**: pushes the branch if needed and opens a pull request into the mission's base branch, with
    *your* CLI login: `gh` for GitHub remotes (`gh auth login`), `az repos` for Azure DevOps remotes (`az login` +
    `az extension add --name azure-devops`). The repo is taken from the worktree's own `origin`, so every registered
    repo targets its own host. Title and description are prefilled from the mission (deliverable + commits) and editable;
    an already open PR for the branch is reused. Other hosts: push and open the PR by hand.
  - **Several GitHub accounts** (e.g. one per client/org): use an SSH host alias in the repo URL for the push
    (`git@github-client:org/repo.git`, see `~/.ssh/config`) and set `gh_user: <account>` on the repo in `repos.yaml`.
    Log that account into gh once (`gh auth login`); PRs then run as that account via `gh auth token --user` –
    no `gh auth switch`.
    `GH_BIN` / `AZ_BIN` override the CLI paths.
- **Leftovers and cleanup:**
  - Uncommitted leftovers are auto-committed at the end of a round.
  - Feedback rounds reuse the same worktree and branch.
  - Dismissing the mission removes the worktree. Branches with commits stay.

## Mission reports

Open **📑 Report** on any mission (or the **📑 Reports** tab) to see the after-action report:

- **📜 Deliverable:** the final result rendered as Markdown, per feedback round (R1, R2 …), with copy and download.
- **📥 Input & feedback:** the objective, the orders (formation, region, A2A context), a timeline of all rounds with
  tokens, cost and duration, and a box to send the next round of feedback.
- **🧩 Artifacts:** the delegation tree of the round. For every unit you see the orders it received (A2A input) and the artifact
  it delivered (A2A output), plus model, tokens, cost and duration.
- **👥 Team:** every unit involved.

Reports are built from the A2A tasks in the mission's `contextId`, so they show exactly what was exchanged. They are also available as
`GET /api/topics/{id}/report` (JSON) and `GET /api/topics/{id}/report.md` (full Markdown export incl. all artifacts).

## The map

- Drag to pan, scroll to zoom, double-click a territory to focus it. `F` fits everything, `1`–`9` jump to missions, `Esc` deselects.
- Click a unit to see its orders, report, model, tokens/cost, XP and link to its agent card. Click a banner to focus its mission.
- Supply lines connect parents and children. 📜 scrolls are A2A requests, ✉️ are responses, 💥 are errors.
- Rings show status: blue = working, spinning violet = thinking, pulsing amber = delegating, clock = waiting, green = done.
- `/#focus=latest` opens the map on the newest mission.

## Persistence (SQLite)

Everything survives a restart: regions, missions (incl. feedback rounds, intel, linked knowledge bases), agents
(level, XP, memory, position), all A2A tasks (so reports stay complete), stats.

- **One file:** `data/landscape.db` (`DB_PATH`, `--db`, `--db off`). Mock mode uses `data/landscape-mock.db`, so demo
  missions never mix with real ones.
- **Write-behind:** the runtime stays in memory; changes are batched and written about once per second in one transaction (WAL mode).
- **Graceful shutdown:** on Ctrl+C the state is saved *before* running work is cancelled. Missions that were running come back
  **⏸️ interrupted** with a **▶️ Resume** button, which re-tasks the same team.
- **Full-text search** (FTS5) over all deliverables and agent artifacts: 📑 Reports tab, or `GET /api/search?q=`.
- **Schema:** each object is stored as a JSON document plus a few indexed columns, so new fields need no migration. Schema
  changes go into `storage/db.py: MIGRATIONS` (tracked via `PRAGMA user_version`). Only `storage/db.py` contains SQL,
  which keeps a later switch to Postgres contained.

## Configuration (`.env`)

| Variable | Default | |
|---|---|---|
| `DB_PATH` | `data/landscape.db` | SQLite persistence file, `off` = in-memory only |
| `PROFILES_DIR` | `profiles` | Your agent profiles (override / extend built-ins), `off` = read-only built-ins |
| `SKILLS_DIRS` | `skills:.github/skills:.claude/skills` | Skill folders (`:`-separated, first wins on duplicate names) |
| `REPOS_FILE` | `repos.yaml` | Registry of git repos agents can work on, `off` = disabled |
| `WORKSPACE_DIR` | `workspace` | Clones + per-mission git worktrees (gitignored) |
| `HOST` / `PORT` / `PUBLIC_URL` | `127.0.0.1` / `8765` / `http://127.0.0.1:$PORT` | `PUBLIC_URL` is the URL agents use to reach each other |
| `VECTOR_STORE_URL` / `VECTOR_STORE_KEY` | – | Knowledge bases for agents (see below) |
| `VECTOR_STORE_PREFIX` / `VECTOR_STORE_PARSER` / `VECTOR_STORE_AUTO_ARCHIVE` | `al` / `markitdown` / `true` | Store name prefix, upload parser, archive finished missions |
| `VECTOR_STORE_RECALL` / `VECTOR_STORE_RECALL_MIN_SCORE` | `5` / `0` | Related past missions Command HQ recalls for a new mission (0 = off), minimum score |
| `MISSION_ARCHIVE_DIR` | `mission-archive` | Exported mission reports imported into the region knowledge bases on startup (`off` = disabled) |
| `DEFAULT_MODEL` / `ORCHESTRATOR_MODEL` | `gpt-4o-mini` / – | LiteLLM model names |
| `LITELLM_PROXY_URL` / `LITELLM_PROXY_API_KEY` | – | Route all calls through a LiteLLM Proxy |
| `MOCK_LLM` | `auto` | `auto` = mock unless a proxy or provider key is configured |
| `CLAUDE_CODE_ENABLED` | off | Enable the local `claude-code/*` provider (see *Claude Code* above) |
| `CLAUDE_CODE_BIN` | `claude` | Path to the `claude` CLI |
| `CLAUDE_CODE_MODELS` | `sonnet,opus,haiku` | Models offered as `claude-code/<name>` |
| `CLAUDE_CODE_TIMEOUT` | `300` | Seconds per CLI call |
| `CLAUDE_CODE_CONCURRENCY` | `2` | Max parallel CLI processes |
| `CLAUDE_CODE_ALLOW_REMOTE` | off | Allow the provider when `HOST` is not loopback (not recommended) |
| `CLAUDE_CODE_TOOLS` | on | Landscape tools for `claude-code/*` agents via the loopback MCP bridge (`0` = text-only) |
| `CLAUDE_CODE_SETTING_SOURCES` | `project` | The CLI's `--setting-sources` (empty = `project`). The default skips `~/.claude/CLAUDE.md` and `~/.claude/settings.json`; including `user` logs a warning |
| `MAX_DEPTH` / `MAX_SUBTASKS` | `3` / `4` | Hierarchy depth and plan width |
| `DEFAULT_SWARM_SIZE` / `MAX_SWARM_SIZE` / `MAX_AGENTS` | `3` / `6` / `80` | Swarm and safety limits |

## Development

```bash
.venv/bin/python -m pytest -q   # e2e: real server, missions via A2A, streaming, cancel propagation, JSON-RPC errors
```

```
src/agent_landscape/
  a2a/        types.py (spec models) · server.py (JSON-RPC router) · client.py · task_manager.py
  agents/     base.py (Agent = A2A executor + LLM + delegation) · units.py · roles.py · mock.py
  runtime/    runtime.py (registry, territories, telemetry) · persistence.py · regions.py · reports.py · hexgrid.py
  context/    intel.py · sandbox.py · toolbox.py · mcp.py · vectorstore.py
  storage/    db.py (SQLite schema, migrations, FTS – the only module with SQL)
  web/        app.py (FastAPI, REST, WebSocket) · static/ (index.html, map.js, app.js, composer.js, report.js, md.js)
  llm.py      LiteLLM wrapper (direct / proxy / mock)
```

Next steps: auth on the A2A endpoints, push notifications, A2A 1.0 wire format. For persistence follow-ups, see
`features.log` (“LATER – persistence”).

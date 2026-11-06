from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

_PROVIDER_KEYS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "AZURE_API_KEY",
    "MISTRAL_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
)


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def _db_path(raw: str) -> str | None:
    raw = raw.strip()
    if raw.lower() in ("", "off", "none", "false", "0"):
        return None
    return str(Path(raw).expanduser().resolve())


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8765
    public_url: str = ""

    default_model: str = "gpt-4o-mini"
    orchestrator_model: str | None = None
    proxy_url: str | None = None
    proxy_key: str | None = None
    mock: bool = True
    mock_delay: tuple[float, float] = (1.2, 3.5)

    max_depth: int = 3
    max_subtasks: int = 4
    default_swarm_size: int = 3
    max_swarm_size: int = 6
    max_agents: int = 80

    organization: str = "Agent Landscape"
    # folders/files agents may read when attached to a mission (read-only sandbox)
    context_roots: list[str] = field(default_factory=lambda: [os.path.expanduser("~")])
    max_tool_steps: int = 6
    # run_check (tests / coverage / lint / dead_code in mission worktrees): interpreter + timeout per check
    checks_python: str | None = None  # None = this server's interpreter
    checks_timeout: float = 300.0
    # vector stores (knowledge bases); None = disabled (mock mode uses an in-memory store)
    vector_url: str | None = None
    vector_key: str | None = None
    vector_parser: str = "markitdown"
    vector_prefix: str = "al"
    vector_auto_archive: bool = True
    # Command HQ recall: top-N related past missions (all region archives) briefed to new missions; 0 = off
    vector_recall: int = 5
    vector_recall_min_score: float = 0.0
    # folder of exported mission reports (*.md) imported into the region knowledge stores on startup; None = off
    mission_archive_dir: str | None = None
    # SQLite file for persistence; None = in-memory only (default for code/tests; from_env enables it)
    db_path: str | None = None
    # agent profiles (prompts/model/tools): built-ins + overrides in this folder; None = built-ins only
    profiles_dir: str | None = None
    # skill roots (folders containing <skill>/SKILL.md)
    skills_dirs: list[str] = field(default_factory=list)
    # local, opt-in claude-code/* provider: runs the user's own `claude` CLI on their subscription seat
    # (no Anthropic API key). Personal-seat only → loopback hosts unless explicitly overridden.
    claude_code_enabled: bool = False
    claude_code_bin: str = "claude"
    claude_code_models: list[str] = field(default_factory=lambda: ["sonnet", "opus", "haiku"])
    claude_code_timeout: int = 300
    claude_code_concurrency: int = 2
    claude_code_allow_remote: bool = False
    # value for the CLI's --setting-sources. "project" = only the (empty) temp cwd, so the user's
    # ~/.claude/CLAUDE.md and ~/.claude/settings.json (env, apiKeyHelper, hooks) are NOT loaded.
    # Managed (enterprise) policy always applies. Empty/unset also means "project" – never the CLI default.
    claude_code_setting_sources: str = "project"
    # landscape tools for claude-code/* agents via a loopback MCP bridge (their toolbox, our sandbox + logging)
    claude_code_tools: bool = True
    # git repos agents can work on: registry file (repos.yaml) + workspace for clones and mission worktrees
    repos_file: str | None = None
    workspace_dir: str = field(default_factory=lambda: str(Path("workspace").resolve()))
    # operations team: always-on, read-only monitors of Outlook / Teams that propose missions.
    # "off" = no ops team (default for code/tests), "mock" = simulated message feed (from_env default)
    ops_source: str = "off"
    ops_poll_seconds: float = 15.0
    ops_mock_interval: float = 120.0  # mock feed: mean seconds between new messages per channel
    # "folder" source: JSON files written by Power Automate flows into a synced OneDrive folder
    # (<dir>/mail, <dir>/teams/channels, <dir>/teams/chats). None = auto-detect OneDrive*/AgentInbox
    ops_inbox_dir: str | None = None
    ops_backlog_hours: float = 24.0  # first start: also triage files of the last N hours (0 = only new ones)
    ops_ignore_senders: list[str] = field(default_factory=list)  # e.g. your own name – never forwarded
    # teams (teams.yaml): saved lead + specialists + repos + standing orders, optionally on a cron schedule
    teams_file: str | None = None  # None = no teams (default for code/tests; from_env: teams.yaml)
    teams_scheduler: bool = False  # run scheduled teams automatically (from_env default on)
    teams_catch_up_hours: float = 12.0  # after downtime: run a missed schedule once if not older than this
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.public_url:
            self.public_url = f"http://127.0.0.1:{self.port}"
        self.public_url = self.public_url.rstrip("/")

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        load_dotenv()
        proxy_url = os.getenv("LITELLM_PROXY_URL") or None
        claude_code_enabled = _bool("CLAUDE_CODE_ENABLED")
        mock_env = os.getenv("MOCK_LLM", "auto").strip().lower()
        if mock_env in ("1", "true", "yes", "on"):
            mock = True
        elif mock_env in ("0", "false", "no", "off"):
            mock = False
        else:
            mock = not (proxy_url or claude_code_enabled or any(os.getenv(k) for k in _PROVIDER_KEYS))
        claude_code_models = [m.strip() for m in os.getenv("CLAUDE_CODE_MODELS", "").split(",") if m.strip()]

        values = dict(
            host=os.getenv("HOST", "127.0.0.1"),
            port=_int("PORT", 8765),
            public_url=os.getenv("PUBLIC_URL", ""),
            default_model=os.getenv("DEFAULT_MODEL", "gpt-4o-mini"),
            orchestrator_model=os.getenv("ORCHESTRATOR_MODEL") or None,
            proxy_url=proxy_url.rstrip("/") if proxy_url else None,
            proxy_key=os.getenv("LITELLM_PROXY_API_KEY") or None,
            mock=mock,
            max_depth=_int("MAX_DEPTH", 3),
            max_subtasks=_int("MAX_SUBTASKS", 4),
            default_swarm_size=_int("DEFAULT_SWARM_SIZE", 3),
            max_swarm_size=_int("MAX_SWARM_SIZE", 6),
            max_agents=_int("MAX_AGENTS", 80),
            max_tool_steps=_int("MAX_TOOL_STEPS", 6),
            checks_python=os.getenv("CHECKS_PYTHON") or None,
            checks_timeout=max(10.0, _float("CHECKS_TIMEOUT", 300.0)),
            vector_url=(os.getenv("VECTOR_STORE_URL") or "").rstrip("/") or None,
            vector_key=os.getenv("VECTOR_STORE_KEY") or None,
            vector_parser=os.getenv("VECTOR_STORE_PARSER", "markitdown"),
            vector_prefix=os.getenv("VECTOR_STORE_PREFIX", "al"),
            vector_auto_archive=os.getenv("VECTOR_STORE_AUTO_ARCHIVE", "true").lower() not in ("0", "false", "no", "off"),
            vector_recall=max(0, _int("VECTOR_STORE_RECALL", 5)),
            vector_recall_min_score=_float("VECTOR_STORE_RECALL_MIN_SCORE", 0.0),
            mission_archive_dir=(None if os.getenv("MISSION_ARCHIVE_DIR", "").strip().lower() in ("off", "none")
                                 else str(Path(os.getenv("MISSION_ARCHIVE_DIR") or "mission-archive").expanduser().resolve())),
            db_path=_db_path(os.getenv("DB_PATH", "data/landscape.db")),
            profiles_dir=(None if os.getenv("PROFILES_DIR", "").strip().lower() in ("off", "none")
                          else str(Path(os.getenv("PROFILES_DIR") or "profiles").expanduser().resolve())),
            skills_dirs=[str(Path(p).expanduser().resolve()) for p in
                         os.getenv("SKILLS_DIRS", "skills:.github/skills:.claude/skills").split(os.pathsep) if p.strip()],
            claude_code_enabled=claude_code_enabled,
            claude_code_bin=os.getenv("CLAUDE_CODE_BIN") or "claude",
            claude_code_models=claude_code_models or ["sonnet", "opus", "haiku"],
            claude_code_timeout=_int("CLAUDE_CODE_TIMEOUT", 300),
            claude_code_concurrency=max(1, _int("CLAUDE_CODE_CONCURRENCY", 2)),
            claude_code_allow_remote=_bool("CLAUDE_CODE_ALLOW_REMOTE"),
            claude_code_setting_sources=os.getenv("CLAUDE_CODE_SETTING_SOURCES", "").strip() or "project",
            claude_code_tools=_bool("CLAUDE_CODE_TOOLS", True),
            repos_file=(None if os.getenv("REPOS_FILE", "").strip().lower() in ("off", "none")
                        else str(Path(os.getenv("REPOS_FILE") or "repos.yaml").expanduser().resolve())),
            workspace_dir=str(Path(os.getenv("WORKSPACE_DIR") or "workspace").expanduser().resolve()),
            ops_source=(os.getenv("OPS_SOURCE") or "mock").strip().lower(),
            ops_poll_seconds=max(1.0, _float("OPS_POLL_SECONDS", 15.0)),
            ops_mock_interval=max(5.0, _float("OPS_MOCK_INTERVAL", 120.0)),
            ops_inbox_dir=os.getenv("OPS_INBOX_DIR") or None,
            ops_backlog_hours=max(0.0, _float("OPS_BACKLOG_HOURS", 24.0)),
            ops_ignore_senders=[s.strip() for s in os.getenv("OPS_IGNORE_SENDERS", "").split(",") if s.strip()],
            teams_file=(None if os.getenv("TEAMS_FILE", "").strip().lower() in ("off", "none")
                        else str(Path(os.getenv("TEAMS_FILE") or "teams.yaml").expanduser().resolve())),
            teams_scheduler=os.getenv("TEAMS_SCHEDULER", "on").strip().lower() not in ("0", "false", "no", "off"),
            teams_catch_up_hours=max(0.0, _float("TEAMS_CATCH_UP_HOURS", 12.0)),
        )
        roots = [r.strip() for r in os.getenv("CONTEXT_ROOTS", "").split(os.pathsep) if r.strip()]
        if roots:
            values["context_roots"] = [os.path.expanduser(r) for r in roots]
        explicit_db = "db_path" in overrides or bool(os.getenv("DB_PATH"))
        values.update(overrides)
        if "db_path" in overrides and overrides["db_path"] is not None:
            values["db_path"] = _db_path(str(overrides["db_path"]))
        if values["mock"] and not explicit_db:  # demo data never mixes with real missions
            values["db_path"] = _db_path("data/landscape-mock.db")
        return cls(**values)

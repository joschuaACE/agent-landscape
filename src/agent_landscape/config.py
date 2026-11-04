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
    # vector stores (knowledge bases); None = disabled (mock mode uses an in-memory store)
    vector_url: str | None = None
    vector_key: str | None = None
    vector_parser: str = "markitdown"
    vector_prefix: str = "al"
    vector_auto_archive: bool = True
    # SQLite file for persistence; None = in-memory only (default for code/tests; from_env enables it)
    db_path: str | None = None
    # agent profiles (prompts/model/tools): built-ins + overrides in this folder; None = built-ins only
    profiles_dir: str | None = None
    # skill roots (folders containing <skill>/SKILL.md)
    skills_dirs: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.public_url:
            self.public_url = f"http://127.0.0.1:{self.port}"
        self.public_url = self.public_url.rstrip("/")

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        load_dotenv()
        proxy_url = os.getenv("LITELLM_PROXY_URL") or None
        mock_env = os.getenv("MOCK_LLM", "auto").strip().lower()
        if mock_env in ("1", "true", "yes", "on"):
            mock = True
        elif mock_env in ("0", "false", "no", "off"):
            mock = False
        else:
            mock = not (proxy_url or any(os.getenv(k) for k in _PROVIDER_KEYS))

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
            vector_url=(os.getenv("VECTOR_STORE_URL") or "").rstrip("/") or None,
            vector_key=os.getenv("VECTOR_STORE_KEY") or None,
            vector_parser=os.getenv("VECTOR_STORE_PARSER", "markitdown"),
            vector_prefix=os.getenv("VECTOR_STORE_PREFIX", "al"),
            vector_auto_archive=os.getenv("VECTOR_STORE_AUTO_ARCHIVE", "true").lower() not in ("0", "false", "no", "off"),
            db_path=_db_path(os.getenv("DB_PATH", "data/landscape.db")),
            profiles_dir=(None if os.getenv("PROFILES_DIR", "").strip().lower() in ("off", "none")
                          else str(Path(os.getenv("PROFILES_DIR") or "profiles").expanduser().resolve())),
            skills_dirs=[str(Path(p).expanduser().resolve()) for p in
                         os.getenv("SKILLS_DIRS", "skills:.github/skills:.claude/skills").split(os.pathsep) if p.strip()],
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

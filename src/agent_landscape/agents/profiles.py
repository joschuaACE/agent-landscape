"""Agent profiles: system instructions, prompt templates, model and tool access per agent type.

A profile is a Markdown file with YAML frontmatter (same idea as Claude Code subagents / VS Code
``.agent.md``); the body is the system prompt:

    ---
    key: researcher            # unique id (file name without .md)
    kind: worker               # worker | orchestrator | swarm_leader | swarm_member
    title: Scout
    icon: 🔭
    description: Gathers facts, options and prior art.   # shown to orchestrators when they plan
    model: bc2_claude-haiku-5-5   # optional, overrides DEFAULT_MODEL (a mission model still wins)
    temperature: 0.7              # optional
    tools: [intel, files, knowledge, mcp, skills]        # tool families this agent may use
    grant_mcp: [tavily_search_mcp]                       # MCP servers/tools it always gets
    skills: ["*"]                                        # skills it may load ("*" = all)
    prompts:                                             # extra templates (orchestrator / swarm leader)
      plan: ...
      synthesize: ...
    enabled: true
    ---
    You are a meticulous research scout ...

Built-in defaults ship with the package (``agents/profiles/``); files in ``PROFILES_DIR`` (default
``profiles/`` in the repo) override them by key or add new ones. Changes on disk are picked up
automatically.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..frontmatter import parse_frontmatter

log = logging.getLogger(__name__)

BUILTIN_DIR = Path(__file__).parent / "profiles"
KINDS = ("worker", "orchestrator", "swarm_leader", "swarm_member")
TOOL_FAMILIES: dict[str, str] = {
    "intel": "Read documents attached to the mission (read_intel)",
    "files": "Read attached folders (list_dir, read_file, search_files)",
    "knowledge": "Search knowledge bases linked to the mission (kb_search)",
    "knowledge_admin": "Create, link and fill knowledge bases (kb_catalog, kb_link, kb_create, kb_add, kb_ingest)",
    "mcp": "MCP tools attached to the mission",
    "skills": "Load skills from the repo (use_skill, read_skill_file)",
}
ALL_FAMILIES = tuple(TOOL_FAMILIES)
DEFAULT_TOOLS: dict[str, list[str]] = {
    "worker": ["intel", "files", "knowledge", "mcp", "skills"],
    "swarm_member": ["intel", "files", "knowledge", "mcp", "skills"],
    "orchestrator": list(ALL_FAMILIES),
    "swarm_leader": [],
}
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")


class _Dumper(yaml.SafeDumper):
    pass


def _str_repr(dumper: yaml.SafeDumper, data: str) -> yaml.Node:
    style = "|" if "\n" in data else None  # multi-line prompts stay readable in git
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_Dumper.add_representer(str, _str_repr)


def _list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    return [str(x).strip() for x in v if str(x).strip()]


@dataclass
class Profile:
    key: str
    kind: str
    title: str
    icon: str = "🤖"
    description: str = ""
    prompt: str = ""
    model: str | None = None
    temperature: float | None = None
    tools: list[str] = field(default_factory=list)
    grant_mcp: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=lambda: ["*"])
    prompts: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    source: str = "builtin"  # builtin | repo | override
    path: str | None = None

    @property
    def rev(self) -> str:
        """Short content hash – shows in reports which version of the instructions an agent used."""
        payload = repr((self.prompt, self.prompts, self.model, self.temperature, self.tools, self.grant_mcp,
                        self.skills))
        return hashlib.sha1(payload.encode()).hexdigest()[:7]

    def allows_skill(self, name: str) -> bool:
        return "*" in self.skills or name in self.skills

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "rev": self.rev}

    def to_markdown(self) -> str:
        meta: dict[str, Any] = {"key": self.key, "kind": self.kind, "title": self.title, "icon": self.icon,
                                "description": self.description}
        for k in ("model", "temperature"):
            if getattr(self, k) is not None:
                meta[k] = getattr(self, k)
        meta["tools"] = self.tools
        if self.grant_mcp:
            meta["grant_mcp"] = self.grant_mcp
        meta["skills"] = self.skills
        if self.prompts:
            meta["prompts"] = {k: v for k, v in self.prompts.items() if v}
        if not self.enabled:
            meta["enabled"] = False
        fm = yaml.dump(meta, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=110)
        return f"---\n{fm}---\n{self.prompt.strip()}\n"

    @classmethod
    def from_data(cls, meta: dict[str, Any], body: str, *, key: str, source: str, path: str | None) -> "Profile":
        key = str(meta.get("key") or key).strip().lower()
        if not KEY_RE.match(key):
            raise ValueError(f"invalid key '{key}' (lowercase letters, digits, hyphens)")
        kind = str(meta.get("kind") or "worker").strip()
        if kind not in KINDS:
            raise ValueError(f"invalid kind '{kind}' (one of {', '.join(KINDS)})")
        tools = _list(meta.get("tools")) if "tools" in meta else list(DEFAULT_TOOLS[kind])
        unknown = [t for t in tools if t not in TOOL_FAMILIES]
        if unknown:
            raise ValueError(f"unknown tool families {unknown} (valid: {', '.join(TOOL_FAMILIES)})")
        prompts = meta.get("prompts") or {}
        if not isinstance(prompts, dict):
            raise ValueError("prompts must be a mapping, e.g. {plan: ..., synthesize: ...}")
        temp = meta.get("temperature")
        return cls(key=key, kind=kind, title=str(meta.get("title") or key.replace("-", " ").title())[:40],
                   icon=str(meta.get("icon") or "🤖")[:4], description=" ".join(str(meta.get("description") or "").split())[:400],
                   prompt=body.strip(), model=(str(meta["model"]).strip() or None) if meta.get("model") else None,
                   temperature=float(temp) if temp not in (None, "") else None, tools=tools,
                   grant_mcp=_list(meta.get("grant_mcp")), skills=_list(meta.get("skills")) if "skills" in meta else ["*"],
                   prompts={str(k): str(v).strip() for k, v in prompts.items() if v},
                   enabled=bool(meta.get("enabled", True)), source=source, path=path)


class ProfileRegistry:
    def __init__(self, user_dir: str | Path | None, builtin_dir: Path = BUILTIN_DIR) -> None:
        self.user_dir = Path(user_dir).expanduser().resolve() if user_dir else None
        self.builtin_dir = builtin_dir
        self.profiles: dict[str, Profile] = {}
        self.builtin: dict[str, Profile] = {}
        self.errors: list[dict[str, str]] = []
        self._sig: tuple = ()
        self._checked = 0.0
        self.reload()

    # ------------------------------------------------------------ loading
    def _files(self) -> list[Path]:
        out = sorted(self.builtin_dir.glob("*.md"))
        if self.user_dir and self.user_dir.is_dir():
            out += sorted(self.user_dir.glob("*.md"))
        return out

    def _signature(self) -> tuple:
        return tuple((str(p), p.stat().st_mtime_ns) for p in self._files() if p.exists())

    def _load_dir(self, d: Path, source: str) -> dict[str, Profile]:
        out: dict[str, Profile] = {}
        if not d or not d.is_dir():
            return out
        for p in sorted(d.glob("*.md")):
            if p.name.lower() == "readme.md":
                continue
            try:
                meta, body = parse_frontmatter(p.read_text(encoding="utf-8"))
                prof = Profile.from_data(meta, body, key=p.stem, source=source, path=str(p))
                out[prof.key] = prof
            except Exception as exc:  # noqa: BLE001 – a broken file must not break the landscape
                self.errors.append({"path": str(p), "error": str(exc)[:300]})
                log.warning("Invalid profile %s: %s", p, exc)
        return out

    def reload(self) -> None:
        self.errors = []
        self.builtin = self._load_dir(self.builtin_dir, "builtin")
        merged = dict(self.builtin)
        if self.user_dir:
            for key, prof in self._load_dir(self.user_dir, "repo").items():
                if key in self.builtin:
                    prof.source = "override"
                merged[key] = prof
        self.profiles = merged
        self._sig = self._signature()
        self._checked = time.monotonic()

    def refresh(self) -> None:
        """Pick up edits on disk (checked at most every 2 s)."""
        if time.monotonic() - self._checked < 2:
            return
        self._checked = time.monotonic()
        if self._signature() != self._sig:
            self.reload()

    # ------------------------------------------------------------- access
    def get(self, key: str | None) -> Profile | None:
        if not key:
            return None
        self.refresh()
        return self.profiles.get(key)

    def by_kind(self, kind: str, *, enabled_only: bool = True) -> list[Profile]:
        self.refresh()
        return [p for p in self.profiles.values() if p.kind == kind and (p.enabled or not enabled_only)]

    def workers(self) -> list[Profile]:
        return self.by_kind("worker")

    def resolve(self, key: str | None, kind: str, fallback: str) -> Profile:
        """Profile of the given kind, else the fallback key, else the first enabled one of that kind."""
        p = self.get(key)
        if p is not None and p.kind == kind and p.enabled:
            return p
        p = self.get(fallback)
        if p is not None and p.kind == kind:
            return p
        candidates = self.by_kind(kind)
        if not candidates:
            raise RuntimeError(f"No enabled '{kind}' profile")
        return candidates[0]

    # ------------------------------------------------------------- writes
    def save(self, data: dict[str, Any]) -> Profile:
        if self.user_dir is None:
            raise PermissionError("PROFILES_DIR is not configured – profiles are read-only")
        prompt = str(data.get("prompt") or "")
        meta = {k: v for k, v in data.items() if k not in ("prompt", "source", "path", "rev")}
        prof = Profile.from_data(meta, prompt, key=str(data.get("key") or ""), source="repo", path=None)
        existing = self.profiles.get(prof.key)
        if existing is not None and existing.kind != prof.kind:
            raise ValueError(f"'{prof.key}' is a {existing.kind} profile – kind cannot change")
        self.user_dir.mkdir(parents=True, exist_ok=True)
        path = self.user_dir / f"{prof.key}.md"
        path.write_text(prof.to_markdown(), encoding="utf-8")
        self.reload()
        return self.profiles[prof.key]

    def remove(self, key: str) -> str:
        """Delete the repo file: overrides revert to the built-in default, custom profiles disappear."""
        prof = self.profiles.get(key)
        if prof is None or prof.source == "builtin" or not prof.path:
            raise ValueError("Only profiles stored in the repo can be reset or deleted")
        Path(prof.path).unlink(missing_ok=True)
        self.reload()
        return "reset" if key in self.builtin else "deleted"

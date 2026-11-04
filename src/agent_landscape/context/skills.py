"""Skills from the repo: folders with a ``SKILL.md`` (open Agent Skills format, as used by Claude/Copilot).

    skills/
      copilot-studio-topics/
        SKILL.md          ---\\nname: copilot-studio-topics\\ndescription: …\\n---\\n<instructions>
        reference.md      optional extra files, readable on demand
        templates/…

Progressive disclosure: agents only see ``name + description`` in their briefing; the full instructions
are loaded when an agent calls ``use_skill``. Extra files are read with ``read_skill_file`` (read-only,
confined to the skill folder). Several roots can be configured (``SKILLS_DIRS``) – e.g. the repo's
``skills/``, ``.github/skills`` or ``.claude/skills``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..frontmatter import parse_frontmatter
from .sandbox import SKIP_DIRS, is_denied, is_text

log = logging.getLogger(__name__)
MAX_SKILL_CHARS = 30_000
MAX_FILE_CHARS = 20_000


@dataclass
class Skill:
    name: str
    description: str
    path: Path  # folder
    root: str
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def files(self) -> list[str]:
        out = []
        for p in sorted(self.path.rglob("*")):
            rel = p.relative_to(self.path)
            if p.is_file() and p.name != "SKILL.md" and not is_denied(p) and not any(x in SKIP_DIRS for x in rel.parts):
                out.append(str(rel))
            if len(out) >= 100:
                break
        return out

    def instructions(self) -> str:
        _, body = parse_frontmatter((self.path / "SKILL.md").read_text(encoding="utf-8", errors="replace"))
        return body[:MAX_SKILL_CHARS]

    def read(self, rel: str) -> str:
        target = (self.path / rel).resolve()
        if not target.is_relative_to(self.path.resolve()) or not target.is_file() or is_denied(target):
            raise FileNotFoundError(f"'{rel}' is not a file of skill '{self.name}'. Files: {', '.join(self.files) or 'none'}")
        if not is_text(target):
            raise ValueError(f"'{rel}' is binary")
        text = target.read_text(encoding="utf-8", errors="replace")
        return text[:MAX_FILE_CHARS] + (f"\n… (truncated, {len(text):,} chars)" if len(text) > MAX_FILE_CHARS else "")

    def brief(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "path": str(self.path), "root": self.root,
                "files": self.files, "meta": {k: v for k, v in self.meta.items() if k not in ("name", "description")}}


class SkillRegistry:
    def __init__(self, roots: list[str | Path]) -> None:
        self.roots = [Path(r).expanduser().resolve() for r in roots]
        self.skills: dict[str, Skill] = {}
        self.errors: list[dict[str, str]] = []
        self._sig: tuple = ()
        self._checked = 0.0
        self.reload()

    def _manifests(self) -> list[Path]:
        out = []
        for root in self.roots:
            if root.is_dir():
                out += sorted(p for p in root.glob("*/SKILL.md"))
        return out

    def _signature(self) -> tuple:
        return tuple((str(p), p.stat().st_mtime_ns) for p in self._manifests())

    def reload(self) -> None:
        skills: dict[str, Skill] = {}
        self.errors = []
        for manifest in self._manifests():
            try:
                meta, body = parse_frontmatter(manifest.read_text(encoding="utf-8", errors="replace"))
                name = str(meta.get("name") or manifest.parent.name).strip()
                desc = " ".join(str(meta.get("description") or "").split())
                if not desc:
                    raise ValueError("SKILL.md needs a 'description' in its frontmatter")
                if not body:
                    raise ValueError("SKILL.md has no instructions")
                if name in skills:  # first root wins
                    continue
                skills[name] = Skill(name=name, description=desc[:500], path=manifest.parent,
                                     root=str(manifest.parent.parent), meta=meta)
            except Exception as exc:  # noqa: BLE001
                self.errors.append({"path": str(manifest), "error": str(exc)[:300]})
                log.warning("Invalid skill %s: %s", manifest, exc)
        self.skills = skills
        self._sig = self._signature()
        self._checked = time.monotonic()

    def refresh(self) -> None:
        if time.monotonic() - self._checked < 2:
            return
        self._checked = time.monotonic()
        if self._signature() != self._sig:
            self.reload()

    def get(self, name: str) -> Skill | None:
        self.refresh()
        return self.skills.get(name)

    def available(self, allowed: list[str]) -> list[Skill]:
        self.refresh()
        if "*" in allowed:
            return list(self.skills.values())
        return [s for n, s in self.skills.items() if n in allowed]

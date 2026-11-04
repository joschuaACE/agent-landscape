"""Markdown + YAML frontmatter (used by agent profiles and skills)."""

from __future__ import annotations

import re
from typing import Any

import yaml

_FM = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)(.*)\Z", re.S)


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    m = _FM.match(text or "")
    if not m:
        return {}, (text or "").strip()
    meta = yaml.safe_load(m.group(1)) or {}
    if not isinstance(meta, dict):
        raise ValueError("frontmatter must be a mapping")
    return meta, m.group(2).strip()

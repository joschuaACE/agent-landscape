"""Read-only filesystem sandbox for mission context (attached files & folders).

Agents only ever see paths inside the *attached* roots, which in turn must lie inside the
configured ``CONTEXT_ROOTS``. Secrets (.env, keys …) and noise (.git, node_modules …) are hidden.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

DENY_FILES = (".env", ".env.*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "id_rsa*", "id_ed25519*",
              "id_ecdsa*", ".netrc", ".npmrc", ".pypirc", ".git-credentials", "*credentials*.json", "*.kdbx")
SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__", ".mypy_cache",
             ".pytest_cache", ".ruff_cache", ".tox", "dist", "build", ".next", ".nuxt", ".idea", ".gradle",
             "target", ".terraform", ".DS_Store"}
MAX_FILE_BYTES = 1_000_000
MAX_LIST = 300


class SandboxError(ValueError):
    pass


def is_denied(path: Path) -> bool:
    name = path.name
    if any(fnmatch.fnmatch(name.lower(), pat) for pat in DENY_FILES):
        return True
    return any(part in SKIP_DIRS for part in path.parts)


def is_text(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return b"\0" not in fh.read(8192)
    except OSError:
        return False


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f} TB"


def check_allowed(raw: str, allowed_roots: list[str]) -> Path:
    """Resolve a user-supplied path and ensure it is inside one of the configured roots."""
    if not raw or not raw.strip():
        raise SandboxError("Empty path")
    p = Path(os.path.expanduser(raw.strip())).resolve()
    roots = [Path(r).expanduser().resolve() for r in allowed_roots]
    if not any(p == r or p.is_relative_to(r) for r in roots):
        raise SandboxError(f"{p} is outside the allowed context roots ({', '.join(map(str, roots))}). "
                           "Set CONTEXT_ROOTS to allow it.")
    if not p.exists():
        raise SandboxError(f"{p} does not exist")
    if is_denied(p):
        raise SandboxError(f"{p.name} is hidden for safety (secrets / build artefacts)")
    return p


def walk(root: Path, limit: int = 5000):
    """Yield visible files below ``root`` (bounded)."""
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".") or d in (".github",))
        for f in sorted(filenames):
            p = Path(dirpath) / f
            if is_denied(p):
                continue
            yield p
            count += 1
            if count >= limit:
                return


def inspect(p: Path) -> dict:
    """Small summary for the composer (✓ 128 files · 2.3 MB)."""
    if p.is_file():
        size = p.stat().st_size
        return {"kind": "file", "path": str(p), "name": p.name, "size": size, "size_h": human(size),
                "text": is_text(p), "too_big": size > MAX_FILE_BYTES}
    files = list(walk(p, limit=5001))
    size = sum(f.stat().st_size for f in files[:5000] if f.exists())
    exts: dict[str, int] = {}
    for f in files[:5000]:
        exts[f.suffix.lower() or f.name] = exts.get(f.suffix.lower() or f.name, 0) + 1
    top = sorted(exts.items(), key=lambda kv: -kv[1])[:6]
    return {"kind": "dir", "path": str(p), "name": p.name or str(p), "files": min(len(files), 5000),
            "files_capped": len(files) > 5000, "size": size, "size_h": human(size),
            "types": [f"{k} ×{v}" for k, v in top]}


def tree(root: Path, max_entries: int = 60, max_depth: int = 2) -> str:
    """Compact indented tree used in agent briefings."""
    lines: list[str] = []

    def rec(d: Path, depth: int) -> None:
        if len(lines) >= max_entries:
            return
        try:
            entries = sorted(d.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        except OSError:
            return
        for e in entries:
            if len(lines) >= max_entries:
                lines.append("  " * depth + "…")
                return
            if is_denied(e) or (e.is_dir() and (e.name in SKIP_DIRS or e.name.startswith(".")) and e.name != ".github"):
                continue
            lines.append("  " * depth + (f"{e.name}/" if e.is_dir() else e.name))
            if e.is_dir() and depth + 1 < max_depth:
                rec(e, depth + 1)

    rec(root, 0)
    return "\n".join(lines)


class FileSandbox:
    """Read-only view onto the folders attached to one mission."""

    def __init__(self, roots: list[Path]) -> None:
        self.roots = [r.resolve() for r in roots]

    def resolve(self, raw: str | None) -> Path:
        if not self.roots:
            raise SandboxError("No folders are attached to this mission")
        if not raw or raw in (".", "/", ""):
            return self.roots[0]
        p = Path(os.path.expanduser(raw))
        candidates = [p.resolve()] if p.is_absolute() else [(r / p).resolve() for r in self.roots]
        for c in candidates:
            if any(c == r or c.is_relative_to(r) for r in self.roots):
                if is_denied(c):
                    raise SandboxError(f"{raw} is hidden for safety")
                if c.exists():
                    return c
        raise SandboxError(f"{raw} not found inside the attached folders: {', '.join(map(str, self.roots))}")

    def rel(self, p: Path) -> str:
        for r in self.roots:
            if p == r or p.is_relative_to(r):
                return str(p.relative_to(r)) if len(self.roots) == 1 else f"{r.name}/{p.relative_to(r)}"
        return str(p)

    def list_dir(self, path: str | None = None) -> str:
        if not path and len(self.roots) > 1:
            return "Attached folders:\n" + "\n".join(f"{r}/" for r in self.roots)
        d = self.resolve(path)
        if d.is_file():
            return f"{self.rel(d)} is a file ({human(d.stat().st_size)}) – use read_file."
        out = []
        for e in sorted(d.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
            if is_denied(e) or (e.is_dir() and e.name in SKIP_DIRS):
                continue
            out.append(f"{e.name}/" if e.is_dir() else f"{e.name}  ({human(e.stat().st_size)})")
            if len(out) >= MAX_LIST:
                out.append(f"… (truncated at {MAX_LIST} entries)")
                break
        return f"{self.rel(d) or '.'}/\n" + ("\n".join(out) or "(empty)")

    def read_file(self, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        f = self.resolve(path)
        if f.is_dir():
            return self.list_dir(path)
        if f.stat().st_size > MAX_FILE_BYTES * 5:
            raise SandboxError(f"{self.rel(f)} is too large ({human(f.stat().st_size)})")
        if not is_text(f):
            raise SandboxError(f"{self.rel(f)} is a binary file")
        lines = f.read_text(errors="replace").splitlines()
        a = max(1, int(start_line or 1))
        b = min(len(lines), int(end_line or a + 399))
        body = "\n".join(f"{i:>5}  {lines[i - 1]}" for i in range(a, b + 1))
        more = f"\n… ({len(lines) - b} more lines – call again with start_line={b + 1})" if b < len(lines) else ""
        return f"{self.rel(f)} (lines {a}-{b} of {len(lines)})\n{body}{more}"

    def search(self, query: str, path: str | None = None, max_hits: int = 50) -> str:
        if not query:
            raise SandboxError("query is required")
        base = [self.resolve(path)] if path else self.roots
        q = query.lower()
        hits: list[str] = []
        scanned = 0
        for root in base:
            files = [root] if root.is_file() else walk(root, limit=3000)
            for f in files:
                scanned += 1
                if q in f.name.lower():
                    hits.append(f"{self.rel(f)}  (file name)")
                if f.stat().st_size > MAX_FILE_BYTES or not is_text(f):
                    continue
                try:
                    for n, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
                        if q in line.lower():
                            hits.append(f"{self.rel(f)}:{n}: {line.strip()[:200]}")
                            if len(hits) >= max_hits:
                                break
                except OSError:
                    continue
                if len(hits) >= max_hits:
                    break
        tail = f"\n… (stopped at {max_hits} hits)" if len(hits) >= max_hits else ""
        return (f"{len(hits)} hit(s) for '{query}' in {scanned} files:\n" + "\n".join(hits) + tail) if hits \
            else f"No matches for '{query}' ({scanned} files searched)."

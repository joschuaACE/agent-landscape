"""Git repositories as mission context.

* **Registry** – ``repos.yaml`` (REPOS_FILE, versioned with the project): name, remote URL and/or local checkout,
  default branch, description, keywords (used by the composer to suggest repos for a briefing).
* **Checkouts** – repos with only a URL are cloned into ``<WORKSPACE_DIR>/repos/<name>`` (your git credential
  helper / SSH agent does the auth; nothing is stored here). Local checkouts are used in place.
* **Mission worktrees** – every mission gets its own ``git worktree`` under ``<WORKSPACE_DIR>/missions/<topic>/<repo>``:
  read-only missions a detached snapshot, editable missions a fresh branch ``agents/<mission>-<id>``.
  Your own working copy is never touched; agents only commit to the mission branch. Pushing is a human action.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import functools
import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import sandbox

log = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
URL_RE = re.compile(r"^(https?://|ssh://|git://|file://|git@[\w.-]+:|/)[^\s]+$")
MAX_WRITE_BYTES = 1_000_000
MAX_DIFF_CHARS = 200_000
BOT_EMAIL = "agents@agent-landscape.local"


class RepoError(ValueError):
    pass


def slug(text: str, n: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:n].strip("-") or "mission"


async def git(*args: str, cwd: str | Path, timeout: float = 120, check: bool = True,
              stdin: str | None = None) -> str:
    """Run git non-interactively (never waits for a password prompt)."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "LC_ALL": "C"}
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=str(cwd), env=env, stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin.encode() if stdin is not None else None), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RepoError(f"git {args[0]} timed out after {timeout:.0f}s") from None
    if check and proc.returncode != 0:
        msg = (err or out).decode(errors="replace").strip().splitlines()
        raise RepoError(f"git {args[0]} failed: {' / '.join(msg[-3:]) or proc.returncode}")
    return out.decode(errors="replace")


# ------------------------------------------------------------------ registry
@dataclass
class RepoSpec:
    name: str
    url: str | None = None
    path: str | None = None  # existing local checkout (used in place)
    branch: str | None = None  # base branch for missions; None = remote default (origin/HEAD)
    description: str = ""
    keywords: list[str] = field(default_factory=list)
    # GitHub account (logged in via `gh auth login`) used for PRs on this repo – one account per org/client,
    # no `gh auth switch` needed. None = gh's active account.
    gh_user: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, "", [])} | {"name": self.name}

    @classmethod
    def from_data(cls, d: dict[str, Any]) -> "RepoSpec":
        name = str(d.get("name") or "").strip().lower()
        if not NAME_RE.match(name):
            raise RepoError(f"invalid repo name '{name}' (lowercase letters, digits, . _ -)")
        url = (str(d.get("url") or "").strip() or None)
        path = (str(d.get("path") or "").strip() or None)
        if not url and not path:
            raise RepoError("a repo needs a remote URL or a local path")
        if url and (url.startswith("-") or not URL_RE.match(url) or "::" in url):
            raise RepoError("unsupported git URL (use https://, ssh://, git@host:path or a local path)")
        branch = (str(d.get("branch") or "").strip() or None)
        if branch and (branch.startswith("-") or not re.match(r"^[\w./-]+$", branch)):
            raise RepoError("invalid branch name")
        kw = d.get("keywords") or []
        if isinstance(kw, str):
            kw = [k for k in re.split(r"[,\n]", kw)]
        gh_user = str(d.get("gh_user") or "").strip() or None
        if gh_user and not re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,38}$", gh_user):
            raise RepoError("invalid GitHub user name in gh_user")
        return cls(name=name, url=url, path=path, branch=branch, gh_user=gh_user,
                   description=" ".join(str(d.get("description") or "").split())[:400],
                   keywords=[" ".join(str(k).split()).lower() for k in kw if str(k).strip()][:20])


@dataclass
class MissionRepo:
    """A repo checked out for one mission (git worktree)."""

    name: str
    path: str  # worktree
    source: str  # checkout the worktree belongs to
    base: str  # ref the mission started from, e.g. origin/main
    base_sha: str
    write: bool = False
    branch: str | None = None  # mission branch (editable missions)
    url: str | None = None
    description: str = ""
    commits: list[dict[str, Any]] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)  # "M path" vs base (incl. uncommitted)
    pushed: dict[str, Any] | None = None
    gh_user: str | None = None  # GitHub account for PRs (from the registry, see RepoSpec.gh_user)
    pr: dict[str, Any] | None = None  # pull request opened by the human (url, number, host, at)
    checks: list[dict[str, Any]] = field(default_factory=list)  # run_check history (newest last)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MissionRepo":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})

    @property
    def mode(self) -> str:
        return f"editable on branch {self.branch}" if self.write else "read-only snapshot"


class RepoRegistry:
    def __init__(self, file: str | Path | None, workspace: str | Path, allowed_roots: list[str] | None = None) -> None:
        self.file = Path(file) if file else None
        self.workspace = Path(workspace)
        self.allowed_roots = allowed_roots or [os.path.expanduser("~")]
        self.repos: dict[str, RepoSpec] = {}
        self.errors: list[str] = []
        self._mtime = 0.0
        self._locks: dict[str, asyncio.Lock] = {}
        self.reload()

    # -------------------------------------------------------------- file
    def reload(self) -> None:
        self.repos, self.errors = {}, []
        if self.file is None or not self.file.exists():
            self._mtime = 0.0
            return
        self._mtime = self.file.stat().st_mtime
        try:
            data = yaml.safe_load(self.file.read_text()) or {}
        except yaml.YAMLError as exc:
            self.errors.append(f"{self.file.name}: {exc}")
            return
        for raw in data.get("repos") or []:
            try:
                spec = RepoSpec.from_data(raw if isinstance(raw, dict) else {})
                self.repos[spec.name] = spec
            except RepoError as exc:
                self.errors.append(f"{raw.get('name') if isinstance(raw, dict) else raw}: {exc}")

    def refresh(self) -> None:
        mtime = self.file.stat().st_mtime if self.file and self.file.exists() else 0.0
        if mtime != self._mtime:
            self.reload()

    def _write(self) -> None:
        if self.file is None:
            raise PermissionError("No REPOS_FILE configured")
        self.file.parent.mkdir(parents=True, exist_ok=True)
        body = {"repos": [s.to_dict() for s in sorted(self.repos.values(), key=lambda s: s.name)]}
        self.file.write_text("# Git repositories agents can work on (managed by agent-landscape – editable by hand)\n"
                             + yaml.safe_dump(body, sort_keys=False, allow_unicode=True))
        self._mtime = self.file.stat().st_mtime

    def get(self, name: str | None) -> RepoSpec | None:
        self.refresh()
        return self.repos.get(str(name or "").lower())

    async def save(self, data: dict[str, Any]) -> RepoSpec:
        spec = RepoSpec.from_data(data)
        if spec.path:
            p = sandbox.check_allowed(spec.path, self.allowed_roots)  # raises SandboxError
            top = (await git("rev-parse", "--show-toplevel", cwd=p, check=False)).strip()
            if not top:
                raise RepoError(f"{p} is not a git repository")
            spec.path = top
        self.refresh()
        self.repos[spec.name] = spec
        self._write()
        return spec

    def remove(self, name: str) -> bool:
        self.refresh()
        if self.repos.pop(name, None) is None:
            return False
        self._write()
        return True

    def suggest(self, text: str) -> list[str]:
        """Repos whose name or keywords appear in a mission text (composer suggestions)."""
        low = text.lower()
        out = []
        for s in self.repos.values():
            words = [s.name, s.name.replace("-", " ")] + s.keywords
            if any(w and re.search(rf"(?<![\w]){re.escape(w)}(?![\w])", low) for w in words):
                out.append(s.name)
        return out

    # ---------------------------------------------------------- checkouts
    def checkout_dir(self, spec: RepoSpec) -> Path:
        return Path(spec.path) if spec.path else self.workspace / "repos" / spec.name

    def lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    async def ensure_checkout(self, spec: RepoSpec, *, fetch: bool = True) -> tuple[Path, str | None]:
        """Clone (managed repos) or validate (local checkouts); fetch the remote. → (dir, warning)."""
        d = self.checkout_dir(spec)
        warning = None
        async with self.lock(f"checkout:{d}"):
            if spec.path:
                if not (d / ".git").exists():
                    raise RepoError(f"{d} is not a git checkout")
            elif not (d / ".git").exists():
                if not spec.url:
                    raise RepoError("no URL to clone from")
                d.parent.mkdir(parents=True, exist_ok=True)
                if d.exists():
                    shutil.rmtree(d)
                await git("clone", "--no-tags", "--", spec.url, str(d), cwd=d.parent, timeout=900)
                return d, None
            if fetch and (await git("remote", cwd=d, check=False)).split():
                try:
                    await git("fetch", "--prune", "--no-tags", "origin", cwd=d, timeout=300)
                except RepoError as exc:  # offline / no credentials → continue with local refs
                    warning = f"fetch failed, using local refs ({exc})"
        return d, warning

    async def status(self, spec: RepoSpec) -> dict[str, Any]:
        d = self.checkout_dir(spec)
        out: dict[str, Any] = {**spec.to_dict(), "checkout": str(d), "managed": not spec.path,
                               "cloned": (d / ".git").exists()}
        if not out["cloned"]:
            return out
        try:
            out["current_branch"] = (await git("rev-parse", "--abbrev-ref", "HEAD", cwd=d, timeout=10)).strip()
            out["remote"] = (await git("remote", "get-url", "origin", cwd=d, check=False, timeout=10)).strip() or None
            base = await self._base(d, spec, None)
            out["base"] = base
            out["last_commit"] = (await git("log", "-1", "--format=%h %s (%cr)", base, cwd=d, timeout=10)).strip()
            wts = await git("worktree", "list", "--porcelain", cwd=d, timeout=10)
            root = self.workspace / "missions"
            out["mission_worktrees"] = sum(1 for line in wts.splitlines()
                                           if line.startswith("worktree ") and str(root) in line)
        except RepoError as exc:
            out["error"] = str(exc)
        return out

    async def _base(self, d: Path, spec: RepoSpec, ref: str | None) -> str:
        want = ref or spec.branch
        cands = ([f"origin/{want}", want] if want else []) + ["origin/HEAD", "HEAD"]
        for c in cands:
            if (await git("rev-parse", "--verify", "--quiet", f"{c}^{{commit}}", cwd=d, check=False, timeout=10)).strip():
                if c == "origin/HEAD":  # name the real default branch (origin/main) in reports
                    sym = (await git("symbolic-ref", "--short", "refs/remotes/origin/HEAD", cwd=d, check=False)).strip()
                    return sym or c
                return c
        raise RepoError(f"ref '{want}' not found")

    # ---------------------------------------------------------- worktrees
    async def create_worktree(self, spec: RepoSpec, topic_id: str, title: str, *, ref: str | None = None,
                              write: bool = False) -> tuple[MissionRepo, str | None]:
        src, warning = await self.ensure_checkout(spec)
        base = await self._base(src, spec, ref)
        base_sha = (await git("rev-parse", f"{base}^{{commit}}", cwd=src)).strip()
        wt = self.workspace / "missions" / topic_id / spec.name
        if wt.exists():
            await git("worktree", "remove", "--force", str(wt), cwd=src, check=False)
            shutil.rmtree(wt, ignore_errors=True)
        wt.parent.mkdir(parents=True, exist_ok=True)
        branch = None
        async with self.lock(f"checkout:{src}"):
            if write:
                branch = await self._free_branch(src, f"agents/{slug(title)}-{topic_id[-6:]}")
                await git("worktree", "add", "-b", branch, str(wt), base_sha, cwd=src)
            else:
                await git("worktree", "add", "--detach", str(wt), base_sha, cwd=src)
        mr = MissionRepo(name=spec.name, path=str(wt), source=str(src), base=base, base_sha=base_sha,
                         write=write, branch=branch, url=spec.url, description=spec.description,
                         gh_user=spec.gh_user)
        return mr, warning

    async def _free_branch(self, src: Path, name: str) -> str:
        cand, n = name, 2
        while (await git("rev-parse", "--verify", "--quiet", f"refs/heads/{cand}", cwd=src, check=False)).strip():
            cand, n = f"{name}-{n}", n + 1
        return cand

    async def make_writable(self, mr: MissionRepo, title: str, topic_id: str) -> None:
        """Feedback round asks for edits on a read-only mission repo → create the mission branch in place."""
        if mr.write:
            return
        branch = await self._free_branch(Path(mr.source), f"agents/{slug(title)}-{topic_id[-6:]}")
        await git("switch", "-c", branch, cwd=mr.path)
        mr.write, mr.branch = True, branch

    async def remove_worktree(self, mr: MissionRepo) -> None:
        """Drop the worktree; the mission branch is kept if it has commits (review / push later)."""
        src = Path(mr.source)
        if src.exists():
            await git("worktree", "remove", "--force", mr.path, cwd=src, check=False)
            if mr.branch and not mr.commits:
                await git("branch", "-D", mr.branch, cwd=src, check=False)
            await git("worktree", "prune", cwd=src, check=False)
        shutil.rmtree(mr.path, ignore_errors=True)
        with contextlib.suppress(OSError):
            Path(mr.path).parent.rmdir()


# ------------------------------------------------------- mission operations
_LOCKS: dict[str, asyncio.Lock] = {}


def _lock(mr: MissionRepo) -> asyncio.Lock:
    return _LOCKS.setdefault(mr.path, asyncio.Lock())


def resolve_path(mr: MissionRepo, raw: str, *, for_write: bool = False) -> tuple[Path, str]:
    """Confine a repo-relative path to the worktree (accepts an optional '<repo>/' prefix)."""
    rel = str(raw or "").strip().replace("\\", "/").lstrip("/")
    if rel.startswith(f"{mr.name}/"):
        rel = rel[len(mr.name) + 1:]
    if not rel or rel in (".", mr.name):
        raise RepoError("path is required (relative to the repo root)")
    root = Path(mr.path).resolve()
    p = (root / rel).resolve()
    if not p.is_relative_to(root) or p == root:
        raise RepoError(f"{raw} is outside the repository")
    parts = p.relative_to(root).parts
    if ".git" in parts or any(fnmatch.fnmatch(p.name.lower(), pat) for pat in sandbox.DENY_FILES):
        raise RepoError(f"{raw} is protected (git internals / secrets)")
    if for_write and p.is_dir():
        raise RepoError(f"{raw} is a directory")
    return p, "/".join(parts)


async def refresh_changes(mr: MissionRepo) -> list[str]:
    await git("add", "-A", cwd=mr.path)
    out = await git("diff", "--cached", "--name-status", "--no-renames", mr.base_sha, cwd=mr.path)
    mr.changed = [" ".join(line.split("\t", 1)) for line in out.splitlines() if line.strip()][:500]
    return mr.changed


async def write_file(mr: MissionRepo, path: str, content: str) -> str:
    if not mr.write:
        raise RepoError(f"{mr.name} is read-only in this mission")
    data = content.encode()
    if len(data) > MAX_WRITE_BYTES:
        raise RepoError(f"content too large ({len(data):,} bytes, max {MAX_WRITE_BYTES:,})")
    async with _lock(mr):
        p, rel = resolve_path(mr, path, for_write=True)
        existed = p.exists()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        await refresh_changes(mr)
    return f"{'Updated' if existed else 'Created'} {rel} ({len(data):,} bytes) on branch {mr.branch} – not committed yet."


async def edit_file(mr: MissionRepo, path: str, old: str, new: str) -> str:
    if not mr.write:
        raise RepoError(f"{mr.name} is read-only in this mission")
    if not old:
        raise RepoError("old_text is required – read the file first and copy the exact lines to replace")
    async with _lock(mr):
        p, rel = resolve_path(mr, path, for_write=True)
        if not p.exists():
            raise RepoError(f"{rel} does not exist – use write_file to create it")
        text = p.read_text(errors="replace")
        n = text.count(old)
        if n == 0:
            raise RepoError(f"old_text not found in {rel} – re-read the file (whitespace and indentation must match)")
        if n > 1:
            raise RepoError(f"old_text occurs {n}× in {rel} – include more surrounding lines so it is unique")
        p.write_text(text.replace(old, new, 1))
        await refresh_changes(mr)
    return f"Edited {rel} (-{old.count(chr(10)) + 1}/+{new.count(chr(10)) + 1} lines) – not committed yet."


async def delete_file(mr: MissionRepo, path: str) -> str:
    if not mr.write:
        raise RepoError(f"{mr.name} is read-only in this mission")
    async with _lock(mr):
        p, rel = resolve_path(mr, path, for_write=True)
        if not p.exists():
            raise RepoError(f"{rel} does not exist")
        p.unlink()
        await refresh_changes(mr)
    return f"Deleted {rel} – not committed yet."


async def commit(mr: MissionRepo, message: str, author: str) -> str:
    if not mr.write:
        raise RepoError(f"{mr.name} is read-only in this mission")
    message = message.strip()
    if len(message) < 5:
        raise RepoError("a meaningful commit message is required")
    async with _lock(mr):
        await git("add", "-A", cwd=mr.path)
        staged = await git("diff", "--cached", "--name-status", "--no-renames", cwd=mr.path)
        if not staged.strip():
            return "Nothing to commit – no changes since the last commit."
        name = f"{author} (Agent Landscape)"
        await git("-c", f"user.name={name}", "-c", f"user.email={BOT_EMAIL}", "-c", "commit.gpgsign=false",
                  "commit", "--no-verify", "-q", "-F", "-", cwd=mr.path, stdin=message[:4000])
        sha = (await git("rev-parse", "--short", "HEAD", cwd=mr.path)).strip()
        files = [" ".join(line.split("\t", 1)) for line in staged.splitlines() if line.strip()]
        mr.commits.append({"sha": sha, "message": message.splitlines()[0][:200], "author": author,
                           "files": files[:100], "at": time.time()})
        await refresh_changes(mr)
    return f"Committed {sha} on {mr.branch}: {message.splitlines()[0][:120]} ({len(files)} file(s))."


async def diff(mr: MissionRepo, path: str | None = None, stat: bool = False, limit: int = MAX_DIFF_CHARS) -> str:
    args = ["diff", "--cached", "--no-color", "--no-renames"] + (["--stat"] if stat else []) + [mr.base_sha]
    if path:
        args += ["--", resolve_path(mr, path)[1]]
    async with _lock(mr):
        await git("add", "-A", cwd=mr.path)
        out = await git(*args, cwd=mr.path)
    if len(out) > limit:
        out = out[:limit] + f"\n… (diff truncated, {len(out):,} chars total)"
    return out


async def history(mr: MissionRepo, path: str | None = None, limit: int = 20) -> str:
    args = ["log", f"-{max(1, min(limit, 100))}", "--format=%h %ad %an: %s", "--date=short"]
    if path:
        args += ["--", resolve_path(mr, path)[1]]
    return await git(*args, cwd=mr.path)


async def push(mr: MissionRepo) -> str:
    if not mr.write or not mr.branch:
        raise RepoError("only editable mission repos have a branch to push")
    if not mr.commits:
        raise RepoError("no commits on the mission branch yet")
    out = await git("push", "-u", "origin", f"{mr.branch}:{mr.branch}", cwd=mr.path, timeout=300)
    mr.pushed = {"at": time.time(), "branch": mr.branch, "sha": mr.commits[-1]["sha"]}
    return out.strip() or f"Pushed {mr.branch}"


# ------------------------------------------------------------------ pull requests (human action)
# CLIs that open the PR with *your* login (gh: keyring, az: `az login`). Overridable for tests / custom installs.
GH_BIN = os.getenv("GH_BIN", "gh")
AZ_BIN = os.getenv("AZ_BIN", "az")
_GITHUB_RE = re.compile(r"^(?:https?://(?:[^@/]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)"
                        r"(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?/?$")
_AZURE_RE = [
    re.compile(r"^https://(?:[^@/]+@)?dev\.azure\.com/(?P<org>[^/]+)/(?P<project>[^/]+)/_git/(?P<repo>[^/?#]+)"),
    re.compile(r"^(?:ssh://)?git@ssh\.dev\.azure\.com[:/]v3/(?P<org>[^/]+)/(?P<project>[^/]+)/(?P<repo>[^/]+)$"),
    re.compile(r"^https://(?:[^@/]+@)?(?P<org>[\w-]+)\.visualstudio\.com/(?:DefaultCollection/)?"
               r"(?P<project>[^/]+)/_git/(?P<repo>[^/?#]+)"),
]


_SSH_URL_RE = re.compile(r"^(?:ssh://)?(?P<user>[\w.-]+)@(?P<host>[\w.-]+)(?P<sep>[:/])(?P<path>.+)$")
_KNOWN_HOSTS = ("github.com", "ssh.dev.azure.com", "dev.azure.com")


@functools.lru_cache(maxsize=64)
def ssh_hostname(alias: str) -> str:
    """Real host behind an ~/.ssh/config alias (``Host github-datev`` → ``github.com``); the alias itself if none."""
    try:
        out = subprocess.run(["ssh", "-G", alias], capture_output=True, text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return alias
    return next((ln.split(None, 1)[1].strip() for ln in out.splitlines() if ln.lower().startswith("hostname ")), alias)


def canonical_url(url: str) -> str:
    """Rewrite SSH host aliases (one key per GitHub account) to the real host so PR hosts are recognised."""
    m = _SSH_URL_RE.match(url)
    if not m or m["host"] in _KNOWN_HOSTS or m["host"].endswith(".visualstudio.com"):
        return url
    real = ssh_hostname(m["host"])
    return url if real == m["host"] else f"{m['user']}@{real}:{m['path']}"


def pr_target(url: str | None) -> dict[str, str] | None:
    """Which host a remote URL belongs to and how to address it there (None = no PR support)."""
    url = canonical_url((url or "").strip())
    if m := _GITHUB_RE.match(url):
        return {"host": "github", "repo": f"{m['owner']}/{m['repo']}"}
    for rx in _AZURE_RE:
        if m := rx.match(url):
            return {"host": "azure", "org": f"https://dev.azure.com/{m['org']}", "project": m["project"],
                    "repo": m["repo"].removesuffix(".git")}
    return None


async def remote_url(mr: MissionRepo) -> str | None:
    """origin of the worktree (local-checkout repos have no ``url`` in the registry)."""
    try:
        return (await git("remote", "get-url", "origin", cwd=mr.path, timeout=15)).strip() or None
    except RepoError:
        return mr.url


async def _cli(argv: list[str], cwd: str, timeout: float = 120, env_extra: dict[str, str] | None = None) -> str:
    """Run gh / az non-interactively; errors carry the CLI's own message."""
    if shutil.which(argv[0]) is None:
        hint = ("install the GitHub CLI and run `gh auth login`" if argv[0] == GH_BIN else
                "install the Azure CLI + `az extension add --name azure-devops`, then `az login`")
        raise RepoError(f"{argv[0]} not found – {hint}")
    env = {**os.environ, "GH_PROMPT_DISABLED": "1", "NO_COLOR": "1", "AZURE_CORE_NO_COLOR": "1",
           **(env_extra or {})}
    proc = await asyncio.create_subprocess_exec(*argv, cwd=cwd, env=env, stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RepoError(f"{Path(argv[0]).name} timed out after {timeout:.0f}s") from None
    if proc.returncode != 0:
        msg = (err or out).decode(errors="replace").strip().splitlines()
        raise RepoError(f"{Path(argv[0]).name} failed: {' / '.join(msg[-3:]) or proc.returncode}")
    return out.decode(errors="replace")


def base_branch(mr: MissionRepo) -> str:
    return mr.base.removeprefix("origin/") if mr.base.startswith("origin/") else mr.base


async def create_pr(mr: MissionRepo, title: str, body: str, *, draft: bool = False) -> dict[str, Any]:
    """Human approval step: push the mission branch if needed, then open a PR on GitHub (gh) or Azure DevOps (az).

    Reuses an already open PR for the branch instead of failing. Agents never call this."""
    if not mr.write or not mr.branch:
        raise RepoError("only editable mission repos have a branch for a pull request")
    if not mr.commits:
        raise RepoError("no commits on the mission branch yet")
    target = pr_target(await remote_url(mr))
    if target is None:
        raise RepoError("pull requests are supported for GitHub and Azure DevOps remotes only – push and open it by hand")
    title = " ".join(title.split())[:240] or mr.branch
    body = body.strip()[:60_000]
    cli = GH_BIN if target["host"] == "github" else AZ_BIN
    if shutil.which(cli) is None:  # fail before pushing anything
        await _cli([cli], mr.path)
    if not (mr.pushed and mr.pushed.get("sha") == mr.commits[-1]["sha"]):
        await push(mr)
    base = base_branch(mr)
    if target["host"] == "github":
        pr = await _github_pr(mr, target, base, title, body, draft)
    else:
        pr = await _azure_pr(mr, target, base, title, body, draft)
    mr.pr = {**pr, "host": target["host"], "base": base, "at": time.time()}
    return mr.pr


async def gh_env(user: str | None, cwd: str) -> dict[str, str]:
    """GH_TOKEN of a specific logged-in gh account (``gh auth token --user``) – avoids `gh auth switch`."""
    if not user:
        return {}
    try:
        token = (await _cli([GH_BIN, "auth", "token", "--hostname", "github.com", "--user", user], cwd)).strip()
    except RepoError:
        raise RepoError(f"gh is not logged in as '{user}' (gh_user of this repo). Run `gh auth login` once "
                        f"with that account – gh keeps several accounts; no switching needed afterwards.") from None
    return {"GH_TOKEN": token} if token else {}


async def _github_pr(mr: MissionRepo, t: dict[str, str], base: str, title: str, body: str,
                     draft: bool) -> dict[str, Any]:
    env = await gh_env(mr.gh_user, mr.path)
    try:
        existing = json.loads(await _cli([GH_BIN, "pr", "list", "--repo", t["repo"], "--head", mr.branch or "",
                                          "--state", "open", "--json", "url,number"], mr.path, env_extra=env)
                              or "[]")
    except RepoError as exc:
        if "Could not resolve to a Repository" in str(exc):  # gh logged in with another GitHub account
            who = f"gh account '{mr.gh_user}'" if mr.gh_user else "active gh account"
            raise RepoError(f"{exc} – the {who} can't see {t['repo']}. Run `gh auth login` with the account that "
                            f"has access and set `gh_user: <that account>` for repo {mr.name} in repos.yaml "
                            "(or `gh auth switch`), then retry.") from None
        raise
    if existing:
        return {"url": existing[0]["url"], "number": existing[0]["number"], "existing": True}
    argv = [GH_BIN, "pr", "create", "--repo", t["repo"], "--head", mr.branch or "", "--base", base,
            "--title", title, "--body", body or title] + (["--draft"] if draft else [])
    out = await _cli(argv, mr.path, env_extra=env)
    url = next((ln.strip() for ln in reversed(out.splitlines()) if ln.strip().startswith("http")), "")
    if not url:
        raise RepoError(f"gh did not return a PR URL: {out.strip()[-300:]}")
    num = url.rstrip("/").rsplit("/", 1)[-1]
    return {"url": url, "number": int(num) if num.isdigit() else None}


async def _azure_pr(mr: MissionRepo, t: dict[str, str], base: str, title: str, body: str,
                    draft: bool) -> dict[str, Any]:
    common = ["--organization", t["org"], "--project", t["project"], "--repository", t["repo"], "--output", "json"]
    existing = json.loads(await _cli([AZ_BIN, "repos", "pr", "list", *common, "--status", "active",
                                      "--source-branch", mr.branch or ""], mr.path) or "[]")
    if existing:
        data, reused = existing[0], True
    else:
        data = json.loads(await _cli([AZ_BIN, "repos", "pr", "create", *common, "--source-branch", mr.branch or "",
                                      "--target-branch", base, "--title", title,
                                      "--description", body or title] + (["--draft", "true"] if draft else []),
                                     mr.path))
        reused = False
    pid = data.get("pullRequestId")
    url = f"{t['org']}/{t['project']}/_git/{t['repo']}/pullrequest/{pid}"
    return {"url": url, "number": pid, **({"existing": True} if reused else {})}


def info(mr: MissionRepo) -> str:
    lines = [f"Repository {mr.name} – {mr.description or 'no description'}",
             f"Worktree: {mr.path}", f"Base: {mr.base} @ {mr.base_sha[:8]}", f"Mode: {mr.mode}"]
    if mr.commits:
        lines.append("Mission commits:\n" + "\n".join(f"  {c['sha']} {c['author']}: {c['message']}" for c in mr.commits))
    lines.append("Changed vs base: " + (", ".join(mr.changed[:40]) if mr.changed else "nothing yet"))
    lines.append("Top level:\n" + sandbox.tree(Path(mr.path), max_entries=50, max_depth=2))
    return "\n".join(lines)

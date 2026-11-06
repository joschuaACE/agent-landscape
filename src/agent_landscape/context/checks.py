"""Sandboxed quality checks in a mission worktree – the agents' only way to *run* something.

Agents never get a shell. ``run_check`` offers a fixed allow-list of checks; arguments are limited to a repo-relative
target path and (for tests) a pytest ``-k`` expression, so no free-form command line ever reaches a subprocess.

* tests      – ``pytest -q``                                  → pass/fail summary
* coverage   – ``pytest --cov --cov-report=term-missing``     → % per file + missing lines
* lint       – ``ruff check``                                 → findings
* dead_code  – ``vulture --min-confidence 60``                → unused code candidates

Isolation (honest scope: this limits accidents, it is not a security boundary – tests execute repo code):
* runs in the mission worktree (never in your own checkout), one check per worktree at a time, global limit 2
* hard timeout, output capped (head + tail)
* secret-looking env vars are blanked, ``.env`` loading disabled, ``MOCK_LLM=1`` – tests never reach paid APIs
* ``PYTHONPATH=<worktree>/src:<worktree>`` so the worktree's code wins over an editable install
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from .repos import MissionRepo, RepoError, resolve_path

CHECKS: dict[str, str] = {
    "tests": "run the test suite (pytest) – optional target file/folder and -k keyword expression",
    "coverage": "pytest with line coverage – % per file and the missing line numbers",
    "lint": "static lint (ruff) – unused imports/variables, bugs, style",
    "dead_code": "unused functions, classes, methods, variables and imports (vulture, confidence ≥ 60 %)",
}
MODULES = {"tests": "pytest", "coverage": "pytest_cov", "lint": "ruff", "dead_code": "vulture"}
KEYWORD_RE = re.compile(r"^[\w .()\[\]-]{1,120}$")
SECRET_RE = re.compile(r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH|COOKIE|SESSION|PRIVATE", re.I)
SECRET_PREFIXES = ("LITELLM_", "VECTOR_STORE_", "OPENAI", "ANTHROPIC", "AZURE", "AWS_", "GOOGLE", "GEMINI",
                   "MISTRAL", "GROQ", "COHERE", "HF_", "GITHUB_", "GH_", "OPS_")
VULTURE_EXCLUDE = ".venv,venv,env,node_modules,workspace,build,dist,.git"
VULTURE_DECORATORS = "@app.*,@router.*,@pytest.fixture,@*.setter,@property"
MAX_OUTPUT = 11_000
HISTORY = 30

_SEM = asyncio.Semaphore(2)
_LOCKS: dict[str, asyncio.Lock] = {}


def _lock(mr: MissionRepo) -> asyncio.Lock:
    return _LOCKS.setdefault(mr.path, asyncio.Lock())


def clean_env(worktree: Path) -> dict[str, str]:
    env = {k: ("" if SECRET_RE.search(k) or k.upper().startswith(SECRET_PREFIXES) else v)
           for k, v in os.environ.items()}
    paths = [str(worktree / "src")] if (worktree / "src").is_dir() else []
    env.update({
        "PYTHONPATH": os.pathsep.join(paths + [str(worktree)]),
        "PYTHON_DOTENV_DISABLED": "1",  # a parent folder's .env must not leak keys into the tests
        "MOCK_LLM": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "NO_COLOR": "1",
        "CI": "1",
        "GIT_TERMINAL_PROMPT": "0",
    })
    return env


def _target(mr: MissionRepo, raw: Any) -> str | None:
    raw = str(raw or "").strip()
    if not raw or raw in (".", "/", mr.name):
        return None
    p, rel = resolve_path(mr, raw)
    if not p.exists():
        raise RepoError(f"{raw} does not exist in {mr.name}")
    return rel


def build_argv(check: str, mr: MissionRepo, python: str, target: str | None = None,
               keyword: str | None = None) -> list[str]:
    root = Path(mr.path)
    base_pytest = [python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--color=no", "-rfE",
                   "--disable-warnings"]  # warnings would eat the output budget; failures stay in the tail
    if check == "tests":
        argv = base_pytest + ([target] if target else [])
        if keyword:
            if not KEYWORD_RE.match(keyword):
                raise RepoError("keyword may only contain letters, digits, spaces and . ( ) [ ] - _")
            argv += ["-k", keyword]
        return argv
    if check == "coverage":
        src = "src" if (root / "src").is_dir() else "."
        return base_pytest + [f"--cov={src}", "--cov-report=term-missing:skip-covered", "--no-cov-on-fail"] \
            + ([target] if target else [])
    if check == "lint":
        return [python, "-m", "ruff", "check", "--no-cache", "--output-format=concise", target or "."]
    if check == "dead_code":
        default = "src" if (root / "src").is_dir() else "."
        argv = [python, "-m", "vulture", target or default, "--min-confidence", "60",
                "--exclude", VULTURE_EXCLUDE, "--ignore-decorators", VULTURE_DECORATORS]
        if not target and (root / "tests").is_dir():
            argv.insert(4, "tests")  # usages in tests count as usages
        return argv
    raise RepoError(f"unknown check '{check}' (one of {', '.join(CHECKS)})")


def summarize(check: str, code: int, out: str) -> str:
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if check in ("tests", "coverage"):
        total = next((ln for ln in reversed(lines) if ln.startswith("TOTAL")), "")
        res = next((ln.strip("= ") for ln in reversed(lines)
                    if re.search(r"\b(passed|failed|error|no tests ran)\b", ln) and " in " in ln), "")
        pct = re.search(r"(\d+(?:\.\d+)?)%\s*$", total)
        return ", ".join(x for x in (res, f"coverage {pct.group(1)}%" if pct else "") if x) or f"exit {code}"
    if check == "lint":
        found = next((ln for ln in reversed(lines) if re.match(r"Found \d+ error", ln)), "")
        return "no lint findings" if code == 0 else (found or f"exit {code}")
    if check == "dead_code":
        n = sum(1 for ln in lines if re.search(r":\d+: unused ", ln))
        return f"{n} unused-code candidate(s)" if n else ("no dead code found" if code == 0 else f"exit {code}")
    return f"exit {code}"


def clip(out: str, limit: int = MAX_OUTPUT) -> str:
    if len(out) <= limit:
        return out
    head = limit // 4
    return out[:head] + f"\n… ({len(out) - limit:,} chars omitted) …\n" + out[-(limit - head):]


async def run_check(mr: MissionRepo, check: str, *, python: str | None = None, timeout: float = 300,
                    target: Any = None, keyword: Any = None, by: str | None = None) -> str:
    if check not in CHECKS:
        raise RepoError(f"unknown check '{check}' (one of {', '.join(CHECKS)})")
    python = python or sys.executable
    root = Path(mr.path)
    if not root.is_dir():
        raise RepoError(f"worktree of {mr.name} is gone")
    rel = _target(mr, target)
    argv = build_argv(check, mr, python, rel, str(keyword or "").strip() or None)
    probe = await asyncio.create_subprocess_exec(python, "-c", f"import {MODULES[check]}",
                                                 stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    if await probe.wait() != 0:
        raise RepoError(f"{MODULES[check]} is not installed for {python} – "
                        "install the checks extra: uv pip install -e '.[checks]'")
    t0 = time.monotonic()
    async with _SEM, _lock(mr):
        proc = await asyncio.create_subprocess_exec(*argv, cwd=str(root), env=clean_env(root),
                                                    stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                                                    start_new_session=True)
        try:
            raw, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RepoError(f"{check} timed out after {timeout:.0f}s – narrow it with a target") from None
    code = proc.returncode or 0
    out = raw.decode(errors="replace").replace(str(root) + os.sep, "")
    secs = time.monotonic() - t0
    summary = summarize(check, code, out)
    # ruff 1 / vulture 3 = "found something" – a result, not a broken run
    findings = (check == "lint" and code == 1) or (check == "dead_code" and code == 3)
    passed = code == 0
    mr.checks = ([*mr.checks, {"check": check, "target": rel, "ok": passed,
                               "summary": summary, "seconds": round(secs, 1), "by": by,
                               "at": time.time()}])[-HISTORY:]
    cmd = " ".join(Path(a).name if a == python else a for a in argv)
    status = "✅ passed" if passed else ("⚠️ findings" if findings else f"❌ failed (exit {code})")
    return f"{status} · {check} · {summary} · {secs:.1f}s\n$ {cmd}\n\n{clip(out.strip()) or '(no output)'}"

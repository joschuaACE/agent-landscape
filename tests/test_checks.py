"""run_check: allow-listed tests / coverage / lint / dead_code in a mission worktree + lifecycle profiles."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_landscape.agents.profiles import Profile, ProfileRegistry
from agent_landscape.config import Settings
from agent_landscape.context import checks
from agent_landscape.context.intel import MissionIntel
from agent_landscape.context.mcp import MCPGateway
from agent_landscape.context.repos import MissionRepo, RepoError
from agent_landscape.context.toolbox import Toolbox

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def demo(tmp_path, monkeypatch) -> MissionRepo:
    root = tmp_path / "demo"
    (root / "src" / "demopkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "demopkg" / "__init__.py").write_text("")
    (root / "src" / "demopkg" / "mod.py").write_text(
        "import os\n\n\ndef used(x):\n    return x * 2\n\n\ndef never_called_helper(y):\n    return y - 1\n")
    (root / "tests" / "test_mod.py").write_text(
        "import os\nfrom demopkg.mod import used\n\n\n"
        "def test_used():\n    assert used(2) == 4\n\n\n"
        "def test_env_is_scrubbed():\n"
        "    assert os.environ.get('MY_API_KEY') == ''\n"
        "    assert os.environ['MOCK_LLM'] == '1' and os.environ['PYTHON_DOTENV_DISABLED'] == '1'\n")
    monkeypatch.setenv("MY_API_KEY", "sk-secret")
    return MissionRepo(name="demo", path=str(root), source=str(root), base="origin/main", base_sha="0" * 40)


async def test_tests_and_coverage_run_in_worktree(demo):
    out = await checks.run_check(demo, "tests", by="Integrator")
    assert out.startswith("✅ passed · tests · 2 passed"), out
    out = await checks.run_check(demo, "tests", keyword="used and not env")
    assert "1 passed" in out and "1 deselected" in out
    out = await checks.run_check(demo, "coverage")
    assert "coverage" in out.splitlines()[0] and "mod.py" in out and "--cov=src" in out
    assert [c["check"] for c in demo.checks] == ["tests", "tests", "coverage"]
    assert demo.checks[0]["by"] == "Integrator" and demo.checks[0]["ok"]


async def test_failing_tests_are_reported(demo):
    (Path(demo.path) / "tests" / "test_bad.py").write_text("def test_bad():\n    assert 1 == 2\n")
    out = await checks.run_check(demo, "tests", target="tests/test_bad.py")
    assert out.startswith("❌ failed (exit 1)") and "1 failed" in out
    assert demo.checks[-1] == {**demo.checks[-1], "ok": False, "target": "tests/test_bad.py"}


async def test_lint_and_dead_code_find_candidates(demo):
    out = await checks.run_check(demo, "lint")
    assert out.startswith("⚠️ findings") and "F401" in out
    out = await checks.run_check(demo, "dead_code")
    assert "never_called_helper" in out and "used(" not in out.split("\n\n", 1)[1]


async def test_arguments_are_confined(demo):
    with pytest.raises(RepoError, match="outside"):
        await checks.run_check(demo, "tests", target="../../etc")
    with pytest.raises(RepoError, match="does not exist"):
        await checks.run_check(demo, "tests", target="tests/nope.py")
    with pytest.raises(RepoError, match="keyword"):
        await checks.run_check(demo, "tests", keyword="x; rm -rf /")
    with pytest.raises(RepoError, match="unknown check"):
        await checks.run_check(demo, "shell")
    assert checks.build_argv("tests", demo, "py", None, "--co")[-2:] == ["-k", "--co"]  # stays an argument of -k


async def test_toolbox_gates_run_check_by_profile(demo):
    tb = Toolbox(MissionIntel(roots=[Path(demo.path)], repos=[demo]), MCPGateway(Settings(mock=True)))

    def agent(tools):
        return SimpleNamespace(name="Scout", role="worker", skills_used=[],
                               profile=Profile(key="x", kind="worker", title="X", prompt="p", tools=tools))

    reader, checker = agent(["files", "repo"]), agent(["files", "repo", "checks"])
    assert "run_check" not in {s["function"]["name"] for s in tb.specs(reader)}
    out, ok, _ = await tb.call("run_check", {"repo": "demo", "check": "tests"}, reader)
    assert not ok and "not available" in out
    assert "run_check" in {s["function"]["name"] for s in tb.specs(checker)}
    assert "verify with `run_check`" in tb.briefing(checker)
    out, ok, _ = await tb.call("run_check", {"repo": "demo", "check": "tests", "target": "tests/test_mod.py"}, checker)
    assert ok and "2 passed" in out
    assert "Checks run so far: tests tests/test_mod.py → 2 passed" in tb.briefing(checker)


def test_lifecycle_profiles_load():
    reg = ProfileRegistry(REPO_ROOT / "profiles")
    assert not reg.errors, reg.errors
    team = {k: reg.get(k) for k in ("code-janitor", "coverage-scout", "integrator", "feature-steward")}
    assert all(team.values())
    assert "checks" in team["code-janitor"].tools and "repo_write" not in team["code-janitor"].tools
    assert {"repo_write", "checks"} <= set(team["integrator"].tools)
    assert team["feature-steward"].kind == "orchestrator" and "Maturity" in team["feature-steward"].prompts["synthesize"]
    assert team["integrator"].max_tool_steps == 24
    assert "max_tool_steps: 24" in team["integrator"].to_markdown()
    assert reg.get("engineer").max_tool_steps is None  # built-ins keep the global default (rev unchanged)

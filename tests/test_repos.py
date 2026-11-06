"""Git repos as mission context: registry, isolated worktrees, agent edits/commits, review + push."""

from __future__ import annotations

import asyncio
import socket
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import uvicorn

from agent_landscape.agents.profiles import Profile
from agent_landscape.config import Settings
from agent_landscape.context import repos as gitrepo
from agent_landscape.context.intel import MissionIntel
from agent_landscape.context.mcp import MCPGateway
from agent_landscape.context.repos import RepoError, RepoRegistry, RepoSpec
from agent_landscape.context.toolbox import Toolbox
from agent_landscape.web.app import create_app


def sh(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def make_origin(tmp: Path) -> Path:
    """A bare 'remote' with one commit on main (stands in for GitHub / Azure DevOps)."""
    seed = tmp / "seed"
    seed.mkdir()
    sh("init", "-q", "-b", "main", cwd=seed)
    (seed / "topics").mkdir()
    (seed / "topics" / "greeting.yaml").write_text("kind: AdaptiveDialog\ntrigger: hello\nmessage: Hi there\n")
    (seed / "README.md").write_text("# Copilot Studio bot\n")
    (seed / ".env").write_text("SECRET=1\n")
    sh("add", "-A", cwd=seed)
    sh("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init", cwd=seed)
    origin = tmp / "origin.git"
    sh("clone", "-q", "--bare", str(seed), str(origin), cwd=tmp)
    return origin


@pytest.fixture()
def registry(tmp_path):
    origin = make_origin(tmp_path)
    reg = RepoRegistry(tmp_path / "repos.yaml", tmp_path / "ws", [str(tmp_path)])
    return reg, origin


async def test_registry_worktree_edit_commit_push(registry, tmp_path):
    reg, origin = registry
    spec = await reg.save({"name": "bbraun-bot", "url": str(origin), "description": "Copilot Studio topics",
                           "keywords": "copilot studio, b.braun"})
    assert "bbraun-bot" in (tmp_path / "repos.yaml").read_text()
    assert reg.suggest("Add a topic to the B.Braun Copilot Studio agent") == ["bbraun-bot"]
    with pytest.raises(RepoError):
        await reg.save({"name": "bad", "url": "--upload-pack=evil"})

    ro, _ = await reg.create_worktree(spec, "topic-abc123", "Read only", write=False)
    assert not ro.write and ro.branch is None and ro.base == "origin/main"
    with pytest.raises(RepoError, match="read-only"):
        await gitrepo.write_file(ro, "x.md", "nope")

    mr, _ = await reg.create_worktree(spec, "topic-def456", "Meeting transcript topic", write=True)
    assert mr.branch == "agents/meeting-transcript-topic-def456"
    assert Path(mr.path).is_relative_to(tmp_path / "ws" / "missions")
    for bad in ("../outside.md", ".git/config", ".env", "topics/../../escape.md", "sub/id_rsa"):
        with pytest.raises(RepoError):
            await gitrepo.write_file(mr, bad, "x")
    assert gitrepo.resolve_path(mr, "/topics/x.yaml")[1] == "topics/x.yaml"  # leading / = repo root
    with pytest.raises(RepoError, match="not found"):
        await gitrepo.edit_file(mr, "topics/greeting.yaml", "message: Bye", "x")
    out = await gitrepo.edit_file(mr, "bbraun-bot/topics/greeting.yaml", "message: Hi there", "message: Hello!")
    assert "Edited topics/greeting.yaml" in out
    await gitrepo.write_file(mr, "topics/transcript.yaml", "kind: AdaptiveDialog\ntrigger: transcript\n")
    assert sorted(mr.changed) == ["A topics/transcript.yaml", "M topics/greeting.yaml"]
    out = await gitrepo.commit(mr, "feat(topics): add meeting transcript topic", "Engineer Bravo")
    assert out.startswith("Committed") and mr.commits[0]["author"] == "Engineer Bravo"
    assert "Nothing to commit" in await gitrepo.commit(mr, "again please", "x")
    d = await gitrepo.diff(mr)
    assert "+message: Hello!" in d and "transcript.yaml" in d
    assert "Engineer Bravo (Agent Landscape)" in sh("log", "-1", "--format=%an", cwd=Path(mr.path))

    await gitrepo.push(mr)
    assert mr.branch in sh("branch", "--list", cwd=origin) and mr.pushed

    # the managed clone's own checkout was never touched; dropping the worktree keeps the branch (has commits)
    clone = tmp_path / "ws" / "repos" / "bbraun-bot"
    assert sh("status", "--porcelain", cwd=clone) == ""
    await reg.remove_worktree(mr)
    await reg.remove_worktree(ro)
    assert not Path(mr.path).exists() and mr.branch in sh("branch", "--list", cwd=clone)


async def test_local_checkout_is_used_in_place(tmp_path):
    origin = make_origin(tmp_path)
    local = tmp_path / "mine"
    sh("clone", "-q", str(origin), str(local), cwd=tmp_path)
    reg = RepoRegistry(tmp_path / "repos.yaml", tmp_path / "ws", [str(tmp_path)])
    spec = await reg.save({"name": "mine", "path": str(local / "topics")})  # any folder inside → toplevel
    assert spec.path == str(local.resolve()) and spec.url is None
    with pytest.raises(ValueError):
        await RepoRegistry(None, tmp_path / "ws", ["/nonexistent-root"]).save({"name": "x", "path": str(local)})
    mr, _ = await reg.create_worktree(spec, "topic-xyz789", "Change", write=True)
    await gitrepo.write_file(mr, "new.md", "hi")
    await gitrepo.commit(mr, "docs: add note", "Scribe")
    assert mr.branch in sh("branch", "--list", cwd=local) and sh("status", "--porcelain", cwd=local) == ""


async def test_toolbox_repo_tools_respect_profiles(registry):
    reg, origin = registry
    spec = await reg.save({"name": "bot", "url": str(origin)})
    mr, _ = await reg.create_worktree(spec, "topic-tb0001", "Toolbox", write=True)
    intel = MissionIntel(roots=[Path(mr.path)], repos=[mr])
    tb = Toolbox(intel, MCPGateway(Settings(mock=True)), mock=True)

    def agent(tools, role="worker"):
        return SimpleNamespace(name="Agent X", role=role, skills_used=[],
                               profile=Profile(key="x", kind="worker", title="X", prompt="p", tools=tools))

    reader, writer = agent(["files", "repo"]), agent(["files", "repo", "repo_write"])
    assert {"repo_info", "repo_log", "repo_diff", "read_file"} <= {s["function"]["name"] for s in tb.specs(reader)}
    assert "edit_file" not in {s["function"]["name"] for s in tb.specs(reader)}
    out, ok, _ = await tb.call("write_file", {"repo": "bot", "path": "a.md", "content": "x"}, reader)
    assert not ok and "not available" in out
    assert "## Code repositories" in tb.briefing(writer) and "You may change editable repos" in tb.briefing(writer)
    out, ok, _ = await tb.call("read_file", {"path": "topics/greeting.yaml"}, reader)
    assert ok and "trigger: hello" in out
    out, ok, _ = await tb.call("edit_file", {"repo": "bot", "path": "topics/greeting.yaml",
                                             "old_text": "trigger: hello", "new_text": "trigger: hi"}, writer)
    assert ok, out
    out, ok, _ = await tb.call("repo_commit", {"repo": "bot", "message": "fix: shorter trigger"}, writer)
    assert ok and out.startswith("Committed")
    out, ok, _ = await tb.call("repo_diff", {"repo": "bot", "stat": True}, reader)
    assert ok and "greeting.yaml" in out
    out, ok, _ = await tb.call("repo_info", {"repo": "bot"}, reader)
    assert "fix: shorter trigger" in out and "Mission commits" in out


# ------------------------------------------------------------------ pull requests (fake gh / az)
FAKE_CLI = """#!{python}
import json, os, sys
from pathlib import Path
here = Path(__file__).parent
log = here / "calls.jsonl"
with log.open("a") as f:
    f.write(json.dumps(sys.argv + ["GH_TOKEN=" + os.environ.get("GH_TOKEN", "")]) + "\\n")
args = sys.argv[1:]
state = here / "opened"
if args[:2] == ["auth", "token"]:
    user = args[args.index("--user") + 1]
    if user != "datev-bot":
        sys.exit("no oauth token found for github.com account " + user)
    print("tok-" + user)
elif args[:2] == ["pr", "list"]:                      # gh
    print(json.dumps([{{"url": "https://github.com/acme/bot/pull/7", "number": 7}}] if state.exists() else []))
elif args[:2] == ["pr", "create"]:
    state.touch(); print("Creating pull request...\\nhttps://github.com/acme/bot/pull/7")
elif args[:3] == ["repos", "pr", "list"]:           # az
    print(json.dumps([{{"pullRequestId": 42}}] if state.exists() else []))
elif args[:3] == ["repos", "pr", "create"]:
    state.touch(); print(json.dumps({{"pullRequestId": 42}}))
else:
    sys.exit("unexpected: " + " ".join(args))
"""


def _fake_cli(tmp: Path, name: str) -> Path:
    import sys

    d = tmp / f"fake-{name}"
    d.mkdir()
    p = d / name
    p.write_text(FAKE_CLI.format(python=sys.executable))
    p.chmod(0o755)
    return p


def _calls(fake: Path) -> list[list[str]]:
    import json

    log = fake.parent / "calls.jsonl"
    return [json.loads(ln)[1:-1] for ln in log.read_text().splitlines()] if log.exists() else []


def _tokens(fake: Path) -> list[str]:
    import json

    log = fake.parent / "calls.jsonl"
    return [json.loads(ln)[-1] for ln in log.read_text().splitlines()]


@pytest.mark.parametrize(("url", "expected"), [
    ("https://github.com/acme/bot.git", {"host": "github", "repo": "acme/bot"}),
    ("git@github.com:acme/bot.git", {"host": "github", "repo": "acme/bot"}),
    ("ssh://git@github.com/acme/bot", {"host": "github", "repo": "acme/bot"}),
    ("https://acme@dev.azure.com/acme/Bots/_git/copilot-bot",
     {"host": "azure", "org": "https://dev.azure.com/acme", "project": "Bots", "repo": "copilot-bot"}),
    ("git@ssh.dev.azure.com:v3/acme/Bots/copilot-bot",
     {"host": "azure", "org": "https://dev.azure.com/acme", "project": "Bots", "repo": "copilot-bot"}),
    ("https://acme.visualstudio.com/Bots/_git/copilot-bot",
     {"host": "azure", "org": "https://dev.azure.com/acme", "project": "Bots", "repo": "copilot-bot"}),
    ("/srv/git/bot.git", None),
    ("https://gitlab.com/acme/bot.git", None),
])
def test_pr_target(url, expected):
    assert gitrepo.pr_target(url) == expected


def test_pr_target_resolves_ssh_host_alias(monkeypatch):
    """One SSH key per GitHub account via ~/.ssh/config aliases (Host github-datev → github.com)."""
    monkeypatch.setattr(gitrepo, "ssh_hostname", lambda h: {"github-datev": "github.com"}.get(h, h))
    assert gitrepo.pr_target("git@github-datev:datevscm/cdc-agents.git") == {"host": "github",
                                                                            "repo": "datevscm/cdc-agents"}
    assert gitrepo.pr_target("ssh://git@github-datev/datevscm/cdc-agents") == {"host": "github",
                                                                              "repo": "datevscm/cdc-agents"}
    assert gitrepo.pr_target("git@gitlab-work:acme/bot.git") is None


async def _committed_repo(registry, name="bot"):
    reg, origin = registry
    spec = await reg.save({"name": name, "url": str(origin)})
    mr, _ = await reg.create_worktree(spec, "topic-pr0001", "PR mission", write=True)
    await gitrepo.write_file(mr, "topics/new.yaml", "kind: AdaptiveDialog\n")
    await gitrepo.commit(mr, "feat: new topic", "Engineer Bravo")
    return mr, origin


async def test_create_pr_github_pushes_then_reuses_open_pr(registry, tmp_path, monkeypatch):
    mr, origin = await _committed_repo(registry)
    gh = _fake_cli(tmp_path, "gh")
    monkeypatch.setattr(gitrepo, "GH_BIN", str(gh))

    async def fake_remote(_mr):
        return "git@github.com:acme/bot.git"
    monkeypatch.setattr(gitrepo, "remote_url", fake_remote)

    pr = await gitrepo.create_pr(mr, "Add  new\ntopic", "Body text")
    assert pr["url"] == "https://github.com/acme/bot/pull/7" and pr["number"] == 7 and pr["host"] == "github"
    assert mr.pushed and mr.branch in sh("branch", "--list", cwd=origin)  # pushed first
    create = next(c for c in _calls(gh) if c[:2] == ["pr", "create"])
    assert create[create.index("--repo") + 1] == "acme/bot" and create[create.index("--head") + 1] == mr.branch
    assert create[create.index("--base") + 1] == "main" and create[create.index("--title") + 1] == "Add new topic"
    assert "--draft" not in create

    again = await gitrepo.create_pr(mr, "x", "y")
    assert again["existing"] and sum(c[:2] == ["pr", "create"] for c in _calls(gh)) == 1


async def test_create_pr_uses_the_repos_gh_account(registry, tmp_path, monkeypatch):
    mr, _ = await _committed_repo(registry)
    gh = _fake_cli(tmp_path, "gh")
    monkeypatch.setattr(gitrepo, "GH_BIN", str(gh))

    async def fake_remote(_mr):
        return "git@github-datev:datevscm/cdc-agents.git"
    monkeypatch.setattr(gitrepo, "remote_url", fake_remote)
    monkeypatch.setattr(gitrepo, "ssh_hostname", lambda h: "github.com")

    mr.gh_user = "someone-else"
    with pytest.raises(RepoError, match="not logged in as 'someone-else'"):
        await gitrepo.create_pr(mr, "t", "b")
    mr.gh_user = "datev-bot"
    pr = await gitrepo.create_pr(mr, "Teams topic", "Body")
    assert pr["number"] == 7
    pr_calls = [tok for c, tok in zip(_calls(gh), _tokens(gh), strict=True) if c[0] == "pr"]
    assert pr_calls and set(pr_calls) == {"GH_TOKEN=tok-datev-bot"}  # every PR call ran as that account
    assert RepoSpec.from_data({"name": "x", "url": "git@github.com:a/b.git", "gh_user": "datev-bot"}).gh_user
    with pytest.raises(RepoError, match="gh_user"):
        RepoSpec.from_data({"name": "x", "url": "git@github.com:a/b.git", "gh_user": "--evil"})


async def test_create_pr_azure_devops(registry, tmp_path, monkeypatch):
    mr, _ = await _committed_repo(registry)
    az = _fake_cli(tmp_path, "az")
    monkeypatch.setattr(gitrepo, "AZ_BIN", str(az))

    async def fake_remote(_mr):
        return "git@ssh.dev.azure.com:v3/acme/Bots/copilot-bot"
    monkeypatch.setattr(gitrepo, "remote_url", fake_remote)

    pr = await gitrepo.create_pr(mr, "Topic", "Body", draft=True)
    assert pr["url"] == "https://dev.azure.com/acme/Bots/_git/copilot-bot/pullrequest/42" and pr["host"] == "azure"
    create = next(c for c in _calls(az) if c[:3] == ["repos", "pr", "create"])
    assert create[create.index("--target-branch") + 1] == "main" and create[-2:] == ["--draft", "true"]


async def test_create_pr_refusals(registry, tmp_path, monkeypatch):
    reg, origin = registry
    spec = await reg.save({"name": "bot", "url": str(origin)})
    ro, _ = await reg.create_worktree(spec, "topic-pr0002", "Read", write=False)
    with pytest.raises(RepoError, match="editable"):
        await gitrepo.create_pr(ro, "t", "b")
    mr, _ = await reg.create_worktree(spec, "topic-pr0003", "Empty", write=True)
    with pytest.raises(RepoError, match="no commits"):
        await gitrepo.create_pr(mr, "t", "b")
    await gitrepo.write_file(mr, "a.md", "x")
    await gitrepo.commit(mr, "docs: a note", "Scribe")
    with pytest.raises(RepoError, match="GitHub and Azure DevOps"):  # origin is a local bare repo
        await gitrepo.create_pr(mr, "t", "b")
    assert mr.pushed is None  # nothing pushed when no PR is possible

    async def fake_remote(_mr):
        return "https://github.com/acme/bot.git"
    monkeypatch.setattr(gitrepo, "remote_url", fake_remote)
    monkeypatch.setattr(gitrepo, "GH_BIN", str(tmp_path / "no-such-gh"))
    with pytest.raises(RepoError, match="not found"):
        await gitrepo.create_pr(mr, "t", "b")


# ------------------------------------------------------------------ e2e
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def server(tmp_path):
    port = _free_port()
    origin = make_origin(tmp_path)
    settings = Settings(port=port, mock=True, mock_delay=(0.01, 0.05), context_roots=[str(tmp_path)],
                        repos_file=str(tmp_path / "repos.yaml"), workspace_dir=str(tmp_path / "ws"))
    app = create_app(settings)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    deadline = time.time() + 10
    while not srv.started and time.time() < deadline:
        time.sleep(0.05)
    yield app, settings.public_url, origin
    srv.should_exit = True
    th.join(timeout=5)


async def test_mission_with_editable_repo(server):
    app, base, origin = server
    rt = app.state.runtime
    async with httpx.AsyncClient(timeout=60) as http:
        r = await http.put(f"{base}/api/repos/bot", json={"name": "bot", "url": str(origin), "branch": "main"})
        assert r.status_code == 200 and r.json()["cloned"] and r.json()["base"] == "origin/main"
        assert [x["name"] for x in (await http.get(f"{base}/api/repos")).json()["repos"]] == ["bot"]
        body = {"goal": "Add a meeting transcript topic", "title": "Transcript topic", "mode": "hierarchy",
                "context": [{"kind": "repo", "repo": "bot", "write": True}, {"kind": "repo", "repo": "nope"}]}
        topic_id = (await http.post(f"{base}/api/missions", json=body)).json()["id"]
        for _ in range(400):
            t = rt.topics.get(topic_id)
            if t and t.status != "active" and t.root_agent_id:
                break
            await asyncio.sleep(0.05)
        assert t.status == "completed", t.result
        ctx = {c["name"]: c for c in t.context}
        assert ctx["bot"]["ok"] and "editable" in ctx["bot"]["detail"] and not ctx["nope"]["ok"]

        code = (await http.get(f"{base}/api/topics/{topic_id}/repos")).json()["repos"][0]
        assert code["write"] and code["commits"] and code["branch"].startswith("agents/transcript-topic-")
        assert "agent-notes/" in code["diff"]  # mock workers wrote + committed notes
        report = (await http.get(f"{base}/api/topics/{topic_id}/report")).json()
        tools = {c["name"] for r_ in report["rounds"] for a in r_["artifacts"] for c in a["tool_calls"]}
        assert {"repo_info", "write_file", "repo_commit"} <= tools

        assert code["pr_host"] is None  # local bare origin → no PR button
        draft = (await http.get(f"{base}/api/topics/{topic_id}/repos/bot/pr")).json()
        assert draft["title"] == "Transcript topic" and draft["base"] == "main" and "## Commits" in draft["body"]
        r = await http.post(f"{base}/api/topics/{topic_id}/repos/bot/pr", json={"title": "x"})
        assert r.status_code == 400 and "GitHub and Azure DevOps" in r.json()["detail"]

        r = await http.post(f"{base}/api/topics/{topic_id}/repos/bot/push")
        assert r.status_code == 200 and code["branch"] in sh("branch", "--list", cwd=origin)

        wt = Path(code["path"])
        assert (await http.delete(f"{base}/api/topics/{topic_id}")).status_code == 200
        for _ in range(100):
            if not wt.exists():
                break
            await asyncio.sleep(0.05)
        assert not wt.exists()

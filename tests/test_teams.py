"""Teams: cron schedules, catch-up, team runs restricted to their members, approving findings."""

from __future__ import annotations

import socket
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import uvicorn

from agent_landscape.config import Settings
from agent_landscape.teams.cron import Cron, CronError
from agent_landscape.teams.service import TeamService, TeamSpec
from agent_landscape.web.app import create_app

from test_repos import make_origin

REPO_ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ cron
def test_cron_next_and_describe():
    c = Cron.parse("0 12 * * 1-5")
    assert c.describe() == "Mon–Fri 12:00"
    fri = datetime(2026, 11, 6, 12, 0)  # Friday
    assert c.next_after(datetime(2026, 11, 6, 11, 59)) == fri
    assert c.next_after(fri) == datetime(2026, 11, 9, 12, 0)  # → Monday
    assert Cron.parse("*/15 9-17 * * mon,wed").next_after(datetime(2026, 11, 5, 17, 50)) == datetime(2026, 11, 9, 9, 0)
    assert Cron.parse("@daily").describe() == "daily 00:00"
    assert Cron.parse("30 8 * * 0,6").describe() == "weekends 08:30"
    # day-of-month OR day-of-week (Vixie): the 1st or any Monday
    assert Cron.parse("0 0 1 * 1").next_after(datetime(2026, 11, 3)) == datetime(2026, 11, 9)
    assert Cron.parse("0 0 29 2 *").next_after(datetime(2026, 3, 1)) == datetime(2028, 2, 29)
    for bad in ("0 12 * *", "61 * * * *", "0 12 * * 5-1", "x * * * *", "0 0 30 2 *"):
        with pytest.raises(CronError):
            Cron.parse(bad).next_after(datetime(2026, 1, 1))


def test_team_spec_validation():
    t = TeamSpec.from_data({"key": "nightly", "title": "Nightly", "schedule": "0 2 * * *",
                            "repos": ["agent-landscape", {"repo": "x", "write": True}], "goal": "Check {date}"})
    assert t.repos == [{"repo": "agent-landscape", "write": False}, {"repo": "x", "write": True}]
    assert not t.on_request and TeamSpec.from_data({"key": "ask"}).on_request
    with pytest.raises(ValueError):
        TeamSpec.from_data({"key": "Bad Key"})
    with pytest.raises(CronError):
        TeamSpec.from_data({"key": "x", "schedule": "every day"})


# ------------------------------------------------------------- scheduler
def _service(tmp_path, teams_yaml: str, catch_up_hours: float = 12.0):
    (tmp_path / "teams.yaml").write_text(teams_yaml)
    events: list[str] = []
    rt = SimpleNamespace(
        settings=SimpleNamespace(teams_file=str(tmp_path / "teams.yaml"), teams_scheduler=True,
                                 teams_catch_up_hours=catch_up_hours),
        persist=SimpleNamespace(kv=lambda: None), emit=lambda *a, **k: None, topics={},
        log=lambda msg, **k: events.append(msg))
    svc = TeamService(rt)  # type: ignore[arg-type]
    runs: list[tuple[str, str]] = []

    async def fake_run(key, *, prompt=None, trigger="manual"):
        runs.append((key, trigger))

    svc.run = fake_run  # type: ignore[method-assign]
    return svc, runs, events


async def test_scheduler_fires_once_and_catches_up(tmp_path):
    svc, runs, events = _service(tmp_path, "teams:\n- {key: lunch, title: Lunch, goal: go, schedule: '0 12 * * *'}\n"
                                           "- {key: manual, title: Manual}\n")
    ts = lambda *a: datetime(*a).timestamp()  # noqa: E731
    assert await svc.tick(ts(2026, 11, 5, 9, 0)) == []  # first sight: start counting, never fire at once
    assert datetime.fromtimestamp(svc.next_run("lunch")) == datetime(2026, 11, 5, 12, 0)
    assert svc.next_run("manual") is None
    assert await svc.tick(ts(2026, 11, 5, 11, 59)) == []
    assert await svc.tick(ts(2026, 11, 5, 12, 0, 20)) == ["lunch"] and runs[-1] == ("lunch", "schedule")
    assert await svc.tick(ts(2026, 11, 5, 12, 1)) == []  # once per due time
    # server was down over lunch → caught up once (not twice for two missed days)
    assert await svc.tick(ts(2026, 11, 7, 15, 0)) == ["lunch"] and runs[-1] == ("lunch", "catch-up")
    assert len(runs) == 2
    # too old (> catch-up window) → skipped with a log line
    assert await svc.tick(ts(2026, 11, 9, 9, 0)) == [] and "too old" in events[-1]
    svc.set_paused("lunch", True)
    assert await svc.tick(ts(2026, 11, 9, 12, 0, 10)) == [] and svc.next_run("lunch") is None
    svc.set_paused("lunch", False)  # resuming never fires what was missed while paused
    assert svc.next_run("lunch") > time.time()


# ------------------------------------------------------------------ e2e
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


TEAMS = """
teams:
- key: health
  area: 4 · Maintaining
  title: Health check
  icon: 🔧
  lead: orchestrator
  members: [code-janitor, coverage-scout, integrator]
  repos: [{repo: bot, write: false}]
  region: Platform Lifecycle
  schedule: "0 12 * * 1-5"
  goal: "Health check {date}: findings F1, F2 …"
- key: features
  area: 4 · Maintaining
  title: Feature requests
  lead: feature-steward
  members: [integrator]
  repos: [{repo: bot, write: true}]
  goal: "Feature request: {prompt}"
"""


@pytest.fixture()
def server(tmp_path):
    port = _free_port()
    origin = make_origin(tmp_path)
    (tmp_path / "repos.yaml").write_text(f"repos:\n- name: bot\n  url: {origin}\n  branch: main\n")
    (tmp_path / "teams.yaml").write_text(TEAMS)
    settings = Settings(port=port, mock=True, mock_delay=(0.01, 0.03), context_roots=[str(tmp_path)],
                        repos_file=str(tmp_path / "repos.yaml"), workspace_dir=str(tmp_path / "ws"),
                        profiles_dir=str(REPO_ROOT / "profiles"), teams_file=str(tmp_path / "teams.yaml"))
    app = create_app(settings)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    deadline = time.time() + 10
    while not srv.started and time.time() < deadline:
        time.sleep(0.05)
    yield app, settings.public_url
    srv.should_exit = True
    th.join(timeout=5)


async def _wait(rt, topic_id, rounds=1):
    for _ in range(600):
        t = rt.topics.get(topic_id)
        if t and t.status != "active" and t.rounds >= rounds and not t.feedback_queued:
            return t
        time.sleep(0.05)
    raise AssertionError(f"mission {topic_id} did not finish")


async def test_team_runs_and_approved_findings(server):
    app, base = server
    rt = app.state.runtime
    async with httpx.AsyncClient(timeout=60) as http:
        snap = (await http.get(f"{base}/api/teams")).json()
        assert snap["enabled"] and not snap["scheduler"] and not snap["errors"]
        health = next(t for t in snap["teams"] if t["key"] == "health")
        assert health["schedule_label"] == "Mon–Fri 12:00" and health["next_run"]
        assert [p["key"] for p in health["member_profiles"]] == ["code-janitor", "coverage-scout", "integrator"]
        assert next(t for t in snap["teams"] if t["key"] == "features")["on_request"]

        assert (await http.post(f"{base}/api/teams/features/run", json={})).status_code == 400  # needs a prompt
        assert (await http.post(f"{base}/api/teams/nope/run", json={})).status_code == 404

        topic = (await http.post(f"{base}/api/teams/health/run", json={"prompt": "focus on tests"})).json()
        t = await _wait(rt, topic["id"])
        assert t.status == "completed" and t.team == "health" and "focus on tests" in t.goal
        assert rt.regions[t.region_id].name == "Platform Lifecycle"
        workers = [a for a in rt.agents.values() if a.topic_id == t.id and a.role == "worker"]
        assert workers and {a.specialty.key for a in workers} <= {"code-janitor", "coverage-scout", "integrator"}
        mr = rt.intel[t.id].repos[0]
        assert not mr.write

        runs = (await http.get(f"{base}/api/teams")).json()["teams"][0]["runs"]
        assert runs[0]["topic_id"] == t.id and runs[0]["status"] == "completed" and runs[0]["trigger"] == "manual"

        # approve findings → feedback round on the same mission, repo upgraded to an editable branch
        r = await http.post(f"{base}/api/teams/health/runs/{t.id}/implement", json={"findings": "F1, F3"})
        assert r.status_code == 200, r.text
        t = await _wait(rt, t.id, rounds=2)
        assert t.rounds == 2 and "F1, F3" in t.feedback[-1]
        assert rt.intel[t.id].repos[0].write and rt.intel[t.id].repos[0].branch

        assert (await http.post(f"{base}/api/teams/health/pause", json={"paused": True})).json()["paused"]
        assert (await http.get(f"{base}/api/teams")).json()["teams"][0]["next_run"] is None

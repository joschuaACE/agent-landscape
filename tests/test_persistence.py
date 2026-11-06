"""Persistence: state survives a restart; running missions come back 'interrupted' and can be resumed."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from contextlib import contextmanager

import httpx
import uvicorn

from agent_landscape.a2a import A2AClient, Message, TaskState
from agent_landscape.config import Settings
from agent_landscape.storage.db import Database
from agent_landscape.web.app import create_app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def running(db_path, mock_delay=(0.01, 0.05)):
    port = _free_port()
    settings = Settings(port=port, mock=True, mock_delay=mock_delay, db_path=str(db_path))
    app = create_app(settings)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                        timeout_graceful_shutdown=1))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    deadline = time.time() + 10
    while not srv.started and time.time() < deadline:
        time.sleep(0.05)
    try:
        yield app, settings.public_url
    finally:
        srv.should_exit = True
        th.join(timeout=10)


def test_fresh_database_schema(tmp_path):
    db = Database(tmp_path / "x.db")
    assert db.version == 1 and db.fts
    assert db.counts()["topics"] == 0
    db.close()
    assert Database(tmp_path / "x.db").version == 1  # migrations are idempotent


async def test_state_survives_restart(tmp_path):
    db = tmp_path / "landscape.db"
    with running(db) as (app, base):
        rt = app.state.runtime
        async with httpx.AsyncClient(timeout=60) as http:
            client = A2AClient(rt.commander.url, http)
            res = await client.send(Message.of("Plan the Copilot Studio rollout",
                                               metadata={"title": "Copilot rollout", "mode": "hierarchy"}))
            await client.send(Message.of("Add a timeline", context_id=res.context_id))
            topic_id = res.context_id
            before = {
                "topic": rt.topics[topic_id].to_dict(),
                "region": rt.regions[rt.topics[topic_id].region_id].to_dict(),
                "agents": {a.id: (a.name, a.xp, a.tasks_done, len(a.memory)) for a in rt.agents.values()},
                "spawned": rt.stats["agents_spawned"],
                "report": (await http.get(f"{base}/api/topics/{topic_id}/report")).json(),
            }

    with running(db) as (app, base):
        rt = app.state.runtime
        t = rt.topics[topic_id]
        assert t.status == "completed" and t.rounds == 2 and t.result == before["topic"]["result"]
        assert t.center == tuple((before["topic"]["q"], before["topic"]["r"]))
        assert rt.regions[t.region_id].to_dict() == before["region"]
        assert {a.id: (a.name, a.xp, a.tasks_done, len(a.memory)) for a in rt.agents.values()} == before["agents"]
        assert rt.stats["agents_spawned"] == before["spawned"]
        assert rt.commander is rt.agents["commander"]
        async with httpx.AsyncClient(timeout=60) as http:
            report = (await http.get(f"{base}/api/topics/{topic_id}/report")).json()
            assert [r["kind"] for r in report["rounds"]] == ["mission", "feedback"]
            assert len(report["rounds"][0]["artifacts"]) == len(before["report"]["rounds"][0]["artifacts"])
            assert report["totals"]["tokens"] == before["report"]["totals"]["tokens"]
            # full-text search over deliverables + artifacts
            hits = (await http.get(f"{base}/api/search", params={"q": "copilot rollout"})).json()
            assert hits["enabled"] and any(h["topic_id"] == topic_id for h in hits["hits"])
            # the restored world keeps working: veterans are reused, names/territories don't collide
            res = await A2AClient(rt.commander.url, http).send(Message.of(
                "Copilot rollout part 2", metadata={"title": "Copilot rollout 2", "mode": "hierarchy",
                                                    "region_id": t.region_id}))
            assert res.status.state == TaskState.completed
            t2 = rt.topics[res.context_id]
            assert t2.center != t.center and rt.stats["veterans_redeployed"] >= 1
            names = [a.name for a in rt.agents.values()]
            assert len(names) == len(set(names))
            state = (await http.get(f"{base}/api/state")).json()
            assert state["config"]["persistence"]["enabled"] and state["config"]["persistence"]["restored"]["missions"] == 1


async def test_pinned_agent_model_wins_and_survives_restart(tmp_path):
    db = tmp_path / "landscape.db"
    with running(db) as (app, base):
        rt = app.state.runtime
        async with httpx.AsyncClient(timeout=60) as http:
            client = A2AClient(rt.commander.url, http)
            res = await client.send(Message.of("Plan the rollout", metadata={"title": "Rollout", "mode": "hierarchy"}))
            root_id = rt.topics[res.context_id].root_agent_id
            r = await http.patch(f"{base}/api/agents/{root_id}", json={"pinned_model": "pinned-model-x"})
            assert r.status_code == 200 and r.json()["model_source"] == "pinned"
            assert r.json()["model"] == "pinned-model-x" and r.json()["pinned_model"] == "pinned-model-x"
            assert (await http.patch(f"{base}/api/agents/nope", json={})).status_code == 404
            # the next LLM turns of the pinned unit use the pinned model, even in a feedback round
            used: list[tuple[str, str]] = []
            orig = rt.llm.complete

            async def spy(messages, **kw):
                used.append(((kw.get("metadata") or {}).get("agent_id"), kw.get("model")))
                return await orig(messages, **kw)

            rt.llm.complete = spy
            await client.send(Message.of("Add a timeline", context_id=res.context_id))
            rt.llm.complete = orig
            root_models = {m for a, m in used if a == root_id}
            assert root_models == {"pinned-model-x"}, used
            assert any(a != root_id and m != "pinned-model-x" for a, m in used)  # only this unit is pinned

    with running(db) as (app, base):
        rt = app.state.runtime
        assert rt.agents[root_id].pinned_model == "pinned-model-x"
        async with httpx.AsyncClient(timeout=60) as http:
            r = await http.patch(f"{base}/api/agents/{root_id}", json={"pinned_model": None})
            assert r.json()["pinned_model"] is None and r.json()["model_source"] != "pinned"


async def test_running_mission_is_interrupted_and_resumable(tmp_path):
    db = tmp_path / "landscape.db"
    with running(db, mock_delay=(0.6, 0.8)) as (app, base):
        rt = app.state.runtime
        async with httpx.AsyncClient(timeout=30) as http:
            topic_id = (await http.post(f"{base}/api/missions",
                                        json={"goal": "Long research", "title": "Long one", "mode": "hierarchy"})).json()["id"]
        for _ in range(200):  # wait until the team is deployed and working
            if len([a for a in rt.agents.values() if a.topic_id == topic_id]) >= 2:
                break
            await asyncio.sleep(0.05)

        async def _shutdown_signal():  # what Ctrl+C triggers, executed on the server's loop
            rt.prepare_shutdown()

        asyncio.run_coroutine_threadsafe(_shutdown_signal(), rt.loop).result(timeout=5)

    with running(db) as (app, base):
        rt = app.state.runtime
        t = rt.topics[topic_id]
        assert t.status == "interrupted"
        busy = [a for a in rt.agents.values() if a.status in ("working", "thinking", "delegating", "waiting")]
        assert not busy and rt.commander.status == "idle"
        assert all(task.status.state.value in ("completed", "failed", "canceled") for task in rt.tasks.tasks.values())
        async with httpx.AsyncClient(timeout=30) as http:
            r = await http.post(f"{base}/api/topics/{topic_id}/resume")
            assert r.status_code == 200
            assert (await http.post(f"{base}/api/topics/{topic_id}/resume")).status_code == 409
        for _ in range(400):
            if rt.topics[topic_id].status not in ("active", "interrupted"):
                break
            await asyncio.sleep(0.05)
        assert rt.topics[topic_id].status == "completed" and rt.topics[topic_id].result


async def test_dismissed_mission_is_deleted(tmp_path):
    db = tmp_path / "landscape.db"
    with running(db) as (app, base):
        rt = app.state.runtime
        async with httpx.AsyncClient(timeout=60) as http:
            res = await A2AClient(rt.commander.url, http).send(Message.of("Short task", metadata={"mode": "hierarchy"}))
            assert (await http.delete(f"{base}/api/topics/{res.context_id}")).status_code == 200
    with running(db) as (app, _):
        rt = app.state.runtime
        assert res.context_id not in rt.topics
        assert not [a for a in rt.agents.values() if a.topic_id == res.context_id]
        assert not rt.tasks.list(context_id=res.context_id)

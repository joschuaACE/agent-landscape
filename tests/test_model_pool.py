"""Auto model mode: the orchestrator assigns a model per subtask from the mission's pool."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
import uvicorn

from agent_landscape.agents.roles import plan_system_prompt
from agent_landscape.agents.units import Orchestrator
from agent_landscape.config import Settings
from agent_landscape.web.app import create_app

POOL = ["small-model", "big-model"]


def test_plan_prompt_lists_pool_and_marks_tool_less_models() -> None:
    prof = SimpleNamespace(prompt="You plan.", prompts={})
    worker = SimpleNamespace(key="researcher", title="Researcher", tools=[], description="d", prompt="p")
    text = plan_system_prompt(prof, [worker], 3, False, False, [], models=["gpt-x", "claude-code/sonnet"])
    assert '"model": "gpt-x"' in text and '- "claude-code/sonnet" (no tools' in text
    assert '"model"' not in plan_system_prompt(prof, [worker], 3, False, False, [])  # default/fixed: unchanged


def test_pick_model_only_accepts_pool_models() -> None:
    logs: list[str] = []
    rt = SimpleNamespace(topics={"t": SimpleNamespace(model_pool=POOL)}, log=lambda msg, **_: logs.append(msg))
    orch = SimpleNamespace(runtime=rt, topic_id="t", model=None, name="Orch")
    orch._pool = lambda: Orchestrator._pool(orch)
    assert Orchestrator._pick_model(orch, {"model": "big-model"}) == "big-model"
    assert Orchestrator._pick_model(orch, {"model": "evil-model"}) is None and "not in the mission's pool" in logs[0]
    assert Orchestrator._pick_model(orch, {}) is None
    rt.topics["t"].model_pool = []  # default / fixed mode: the planner can't override
    orch.model = "fixed"
    assert Orchestrator._pick_model(orch, {"model": "big-model"}) == "fixed"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def server():
    port = _free_port()
    settings = Settings(port=port, mock=True, mock_delay=(0.01, 0.05), default_model="small-model",
                        orchestrator_model="big-model")
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


async def _finish(rt, topic_id: str):
    for _ in range(400):
        t = rt.topics.get(topic_id)
        if t and t.status != "active" and t.root_agent_id:
            return t
        await asyncio.sleep(0.05)
    raise AssertionError("mission did not finish")


async def test_auto_mission_assigns_pool_models(server):
    app, base = server
    rt = app.state.runtime
    async with httpx.AsyncClient(timeout=60) as http:
        body = {"goal": "Plan a launch", "title": "Auto models", "mode": "hierarchy",
                "model": "auto", "model_pool": ["small-model", "big-model", "unknown-model"]}
        topic = (await http.post(f"{base}/api/missions", json=body)).json()
        assert topic["model_pool"] == POOL  # unknown models are dropped
        t = await _finish(rt, topic["id"])
        assert t.status == "completed", t.result
        report = (await http.get(f"{base}/api/topics/{t.id}/report")).json()
        workers = [a for r in report["rounds"] for a in r["artifacts"] if a["role"] == "worker"]
        assert workers and all(a["model"] in POOL for a in workers)

        fixed = (await http.post(f"{base}/api/missions", json={**body, "model": "small-model"})).json()
        assert fixed["model_pool"] == []  # a forced model wins over the pool
        t = await _finish(rt, fixed["id"])
        report = (await http.get(f"{base}/api/topics/{t.id}/report")).json()
        assert {a["model"] for r in report["rounds"] for a in r["artifacts"] if a["model"]} == {"small-model"}

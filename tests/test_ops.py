"""Operations team: always-on read-only monitors → Communication Orchestrator → Commander → proposal → approval."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from contextlib import contextmanager

import httpx
import uvicorn

from agent_landscape.config import Settings
from agent_landscape.ops.proposals import heuristic_triage
from agent_landscape.ops.sources import MessageSource, MockSource
from agent_landscape.web.app import create_app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def running(db_path=None):
    with running_with(db_path=str(db_path) if db_path else None) as base:
        yield base


@contextmanager
def running_with(**overrides):
    port = _free_port()
    kw = dict(port=port, mock=True, mock_delay=(0.01, 0.05), db_path=None, ops_source="mock",
              ops_mock_interval=3600, ops_poll_seconds=1)
    settings = Settings(**{**kw, **overrides})
    app = create_app(settings)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                        timeout_graceful_shutdown=1))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    deadline = time.time() + 10
    while not srv.started and time.time() < deadline:
        time.sleep(0.05)
    try:
        yield settings.public_url
    finally:
        srv.should_exit = True
        th.join(timeout=10)


async def wait_for(http, base, pred, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        ops = (await http.get(f"{base}/api/ops")).json()
        if pred(ops):
            return ops
        await asyncio.sleep(0.2)
    raise AssertionError(f"timeout – last ops state: {ops}")


def pending(ops):
    return [p for p in ops["proposals"] if p["status"] == "pending"]


def test_sources_are_read_only():
    public = {n for n in dir(MockSource) if not n.startswith("_")}
    assert not {n for n in public if any(w in n for w in ("send", "reply", "post", "delete", "move", "mark"))}
    assert {n for n in dir(MessageSource) if not n.startswith("_")} == {"fetch", "close", "kind"}


def test_mock_feed_avoids_quick_repeats():
    src = MockSource("outlook", seed=3)
    bodies = [src.make().body for _ in range(len(src.pool) - 2)]
    assert len(set(bodies)) == len(bodies)


def test_heuristic_triage():
    src = MockSource("teams", seed=1)
    assert heuristic_triage(src.make(body="can someone review the rate limit config until Friday?"))["relevant"]
    assert not heuristic_triage(src.make(body="lunch at 12?"))["relevant"]
    assert all(heuristic_triage(src.make(True))["relevant"] for _ in range(5))


async def test_team_runs_without_commander_orders_and_proposes(tmp_path):
    with running() as base:
        async with httpx.AsyncClient(timeout=30) as http:
            ops = (await http.get(f"{base}/api/ops")).json()
            assert ops["enabled"] and ops["running"] and ops["mock"]
            assert {m["channel"] for m in ops["monitors"]} == {"outlook", "teams"}
            state = (await http.get(f"{base}/api/state")).json()
            team = {a["id"]: a for a in state["agents"] if a["topic_id"] == "ops"}
            assert set(team) == {"ops-comms", "ops-outlook", "ops-teams"}
            assert team["ops-comms"]["parent_id"] == "commander" and team["ops-outlook"]["parent_id"] == "ops-comms"
            assert all(a["status"] == "monitoring" for a in team.values())
            assert not [t for t in state["topics"] if t["kind"] == "mission"]  # nothing started by itself

            # noise is seen but not forwarded
            await http.post(f"{base}/api/ops/simulate", json={"channel": "teams", "relevant": False})
            ops = await wait_for(http, base, lambda o: o["feed"])
            assert ops["feed"][-1]["relevant"] is False and not ops["proposals"]

            # relevant mail → lead → Commander → pending proposal (no mission yet)
            body = "Could you prepare a comparison of Qdrant and pgvector for our RAG pilot by Friday?"
            await http.post(f"{base}/api/ops/simulate", json={"channel": "outlook", "subject": "RAG pilot database",
                                                               "body": body, "sender": "Lena"})
            ops = await wait_for(http, base, lambda o: pending(o))
            p = pending(ops)[0]
            assert p["kind"] == "mission" and p["channels"] == ["outlook"] and p["messages"][0]["sender"] == "Lena"
            assert p["goal"] and p["note"] and (p["region_id"] or p["new_region"])
            assert p["intel"][0]["kind"] == "file" and body in p["intel"][0]["content"]
            state = (await http.get(f"{base}/api/state")).json()
            assert not [t for t in state["topics"] if t["kind"] == "mission"]

            # follow-up in the same thread is merged into the pending proposal
            await http.post(f"{base}/api/ops/simulate", json={"channel": "outlook", "subject": "Re: RAG pilot database",
                                                               "body": "Please also include Azure AI Search.",
                                                               "sender": "Lena"})
            ops = await wait_for(http, base, lambda o: len(pending(o)[0]["messages"]) == 2)
            assert len(pending(ops)) == 1

            # human approves: mission starts (in the proposed region) and the proposal is closed
            res = await http.post(f"{base}/api/missions", json={
                "goal": p["goal"], "title": p["title"], "region_id": p["region_id"], "new_region": p["new_region"],
                "proposal_id": p["id"], "context": p["intel"]})
            assert res.status_code == 200, res.text
            topic = res.json()
            ops = await wait_for(http, base, lambda o: o["proposals"][0]["status"] == "approved")
            assert ops["proposals"][0]["topic_id"] == topic["id"]
            if p["new_region"]:
                state = (await http.get(f"{base}/api/state")).json()
                assert p["new_region"]["name"] in {r["name"] for r in state["regions"]}

            # another one gets dismissed
            await http.post(f"{base}/api/ops/simulate", json={"channel": "teams", "relevant": True})
            ops = await wait_for(http, base, lambda o: pending(o))
            r = await http.post(f"{base}/api/ops/proposals/{pending(ops)[0]['id']}/dismiss")
            assert r.json()["status"] == "dismissed"
            assert (await http.post(f"{base}/api/ops/proposals/{pending(ops)[0]['id']}/dismiss")).status_code == 409


async def test_ops_state_survives_restart(tmp_path):
    db = tmp_path / "ops.db"
    with running(db) as base:
        async with httpx.AsyncClient(timeout=30) as http:
            await http.post(f"{base}/api/ops/simulate", json={"channel": "outlook", "relevant": True})
            ops = await wait_for(http, base, lambda o: pending(o))
            pid = pending(ops)[0]["id"]
            await asyncio.sleep(1.5)  # write-behind flush
    with running(db) as base:
        async with httpx.AsyncClient(timeout=30) as http:
            ops = (await http.get(f"{base}/api/ops")).json()
            assert [p["id"] for p in pending(ops)] == [pid] and ops["feed"]
            state = (await http.get(f"{base}/api/state")).json()
            team = [a for a in state["agents"] if a["topic_id"] == "ops"]
            assert len(team) == 3 and all(a["status"] == "monitoring" for a in team)

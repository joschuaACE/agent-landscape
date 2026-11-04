"""End-to-end: real uvicorn server, mock LiteLLM, mission driven purely via A2A JSON-RPC."""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from agent_landscape.a2a import A2AClient, Message, Task, TaskState
from agent_landscape.config import Settings
from agent_landscape.web.app import create_app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    port = _free_port()
    root = tmp_path_factory.mktemp("ctx")
    (root / "src").mkdir()
    (root / "src" / "tool.py").write_text("MAGIC_MARKER = 42  # the answer\n")
    (root / "README.md").write_text("# Project\n")
    (root / ".env").write_text("TOKEN=SUPER_SECRET\n")
    settings = Settings(port=port, mock=True, mock_delay=(0.01, 0.05), max_depth=3, context_roots=[str(root)])
    app = create_app(settings)
    app.state.ctx_root = root
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(config)
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    deadline = time.time() + 10
    while not srv.started and time.time() < deadline:
        time.sleep(0.05)
    yield app, settings.public_url
    srv.should_exit = True
    th.join(timeout=5)


async def test_root_agent_card(server):
    _, base = server
    async with httpx.AsyncClient() as http:
        card = (await http.get(f"{base}/.well-known/agent-card.json")).json()
    assert card["protocolVersion"] == "0.3.0"
    assert card["preferredTransport"] == "JSONRPC"
    assert card["url"] == f"{base}/a2a/commander"  # stable URL for LiteLLM registration
    assert card["capabilities"]["streaming"] is True
    assert card["skills"][0]["id"] == "mission"


async def test_mission_via_message_send(server):
    app, base = server
    rt = app.state.runtime
    async with httpx.AsyncClient(timeout=60) as http:
        card = (await http.get(f"{base}/.well-known/agent-card.json")).json()
        client = A2AClient(card["url"], http)
        res = await client.send(Message.of("Plan a product launch", metadata={"mode": "auto"}))
        assert isinstance(res, Task)
        assert res.status.state == TaskState.completed, res.status
        assert res.artifacts and "Final deliverable" in res.artifacts[0].parts[0].text

        # tasks/get round-trip
        again = await client.get_task(res.id, history_length=1)
        assert again.id == res.id and len(again.history) <= 1

    roles = {a.role for a in rt.agents.values()}
    assert {"commander", "orchestrator"} <= roles
    assert roles & {"worker", "swarm_leader"}
    missions = [t for t in rt.topics.values() if t.kind == "mission"]
    assert missions and missions[-1].status == "completed"


async def test_swarm_mode_streaming(server):
    app, base = server
    rt = app.state.runtime
    async with httpx.AsyncClient(timeout=60) as http:
        commander = rt.commander
        client = A2AClient(commander.url, http)
        kinds = []
        async for ev in client.stream(Message.of("Brainstorm names", metadata={"mode": "swarm", "swarm_size": 4})):
            kinds.append(ev.kind)
        assert kinds[0] == "task" and "artifact-update" in kinds and kinds[-1] == "status-update"
    topic = [t for t in rt.topics.values() if t.kind == "mission"][-1]
    members = [a for a in rt.agents.values() if a.topic_id == topic.id and a.role == "swarm_member"]
    assert len(members) == 4


async def test_cancel_propagates(server):
    app, base = server
    rt = app.state.runtime
    rt.settings.mock_delay = (2.0, 2.5)
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            r = await http.post(f"{base}/api/missions", json={"goal": "Long running mission", "mode": "hierarchy"})
            topic = rt.topics[r.json()["id"]]
            for _ in range(250):  # survey (~2s) + plan (~2s) before children exist
                await asyncio.sleep(0.05)
                if topic.task_id and len([a for a in rt.agents.values() if a.topic_id == topic.id]) > 1:
                    break
            r = await http.post(f"{base}/api/topics/{topic.id}/cancel")
            assert r.status_code == 200
        for _ in range(100):
            if topic.status != "active":
                break
            await asyncio.sleep(0.05)
        assert topic.status == "canceled"
        await asyncio.sleep(0.3)
        statuses = {a.status for a in rt.agents.values() if a.topic_id == topic.id}
        assert not statuses & {"working", "thinking", "delegating", "waiting"}, statuses
    finally:
        rt.settings.mock_delay = (0.01, 0.05)


async def test_hq_groups_missions_into_biome_regions(server):
    app, base = server
    rt = app.state.runtime
    async with httpx.AsyncClient(timeout=60) as http:
        client = A2AClient(rt.commander.url, http)
        r1 = await client.send(Message.of("Build a Copilot Studio topic that reads Outlook mails",
                                          metadata={"title": "Copilot Studio mail topic", "mode": "hierarchy"}))
        r2 = await client.send(Message.of("Add Teams meeting transcripts to our Copilot Studio agent",
                                          metadata={"title": "Copilot Studio transcripts", "mode": "hierarchy"}))
        r3 = await client.send(Message.of("Write a concept for a new GenAI platform",
                                          metadata={"title": "GenAI platform concept", "mode": "hierarchy"}))
        t1, t2, t3 = (rt.topics[r.context_id] for r in (r1, r2, r3))
        assert t1.region_id == t2.region_id != t3.region_id
        assert rt.regions[t1.region_id].biome == "desert"
        assert rt.regions[t3.region_id].biome == "meadow"
        assert t1.center != t2.center and None not in (t1.center, t2.center, t3.center)
        # second mission in the region re-deployed the first mission's idle orchestrator
        assert t2.root_agent_id == t1.root_agent_id
        assert rt.agents[t2.root_agent_id].topic_id == t2.id

        # manual override: rename/re-biome a region, and pin a mission to it
        rid = t3.region_id
        r = await http.patch(f"{base}/api/regions/{rid}", json={"name": "GenAI Fields", "biome": "crystal"})
        assert r.status_code == 200 and r.json()["biome"] == "crystal"
        assert (await http.patch(f"{base}/api/regions/hq-region", json={"name": "x"})).status_code == 404
        r = await http.post(f"{base}/api/missions", json={"goal": "Unrelated chores", "region_id": rid})
        topic = rt.topics[r.json()["id"]]
        for _ in range(200):
            if topic.region_id:
                break
            await asyncio.sleep(0.05)
        assert topic.region_id == rid
        state = (await http.get(f"{base}/api/state")).json()
        assert any(x["id"] == rid and x["name"] == "GenAI Fields" for x in state["regions"])


async def test_concurrent_missions_share_new_region(server):
    app, _ = server
    rt = app.state.runtime
    async with httpx.AsyncClient(timeout=60) as http:
        client = A2AClient(rt.commander.url, http)
        a, b = await asyncio.gather(
            client.send(Message.of("Sync Salesforce leads", metadata={"title": "Salesforce sync", "mode": "hierarchy"})),
            client.send(Message.of("Salesforce sync dashboard", metadata={"title": "Salesforce sync dashboard",
                                                                          "mode": "hierarchy"})))
    assert rt.topics[a.context_id].region_id == rt.topics[b.context_id].region_id


async def test_feedback_round_reuses_whole_team(server):
    app, base = server
    rt = app.state.runtime
    async with httpx.AsyncClient(timeout=60) as http:
        client = A2AClient(rt.commander.url, http)
        res = await client.send(Message.of("Draft an onboarding checklist", metadata={"mode": "auto"}))
        topic = rt.topics[res.context_id]
        root_id, spawned = topic.root_agent_id, rt.stats["agents_spawned"]
        team = {a.id for a in rt.agents.values() if a.topic_id == topic.id}

        # feedback = another A2A message in the same contextId
        res2 = await client.send(Message.of("Make it shorter and add a timeline", context_id=res.context_id))
        assert res2.status.state == TaskState.completed, res2.status
        assert topic.rounds == 2 and topic.feedback == ["Make it shorter and add a timeline"]
        assert topic.root_agent_id == root_id
        assert rt.stats["agents_spawned"] == spawned, "feedback round should not spawn new units"
        assert {a.id for a in rt.agents.values() if a.topic_id == topic.id} == team
        root = rt.agents[root_id]
        assert root.tasks_done == 2 and len(root.memory) == 2

        # REST variant; refused while running
        r = await http.post(f"{base}/api/topics/{topic.id}/feedback", json={"text": "Now in German"})
        assert r.status_code == 200
        r = await http.post(f"{base}/api/topics/{topic.id}/feedback", json={"text": "Too early"})
        assert r.status_code == 409
        for _ in range(200):
            if topic.status != "active":
                break
            await asyncio.sleep(0.05)
        assert topic.status == "completed" and topic.rounds == 3


async def test_mission_report_with_rounds_and_artifacts(server):
    app, base = server
    rt = app.state.runtime
    async with httpx.AsyncClient(timeout=60) as http:
        client = A2AClient(rt.commander.url, http)
        res = await client.send(Message.of("Plan a team offsite", metadata={"title": "Offsite", "mode": "auto"}))
        await client.send(Message.of("Cheaper please", context_id=res.context_id))
        topic_id = res.context_id

        report = (await http.get(f"{base}/api/topics/{topic_id}/report")).json()
        assert [r["kind"] for r in report["rounds"]] == ["mission", "feedback"]
        r1, r2 = report["rounds"]
        assert r1["input"] == "Plan a team offsite" and r2["input"] == "Cheaper please"
        assert r1["deliverable"] and r2["deliverable"] and r2["state"] == "completed"
        root = r1["artifacts"][0]
        assert root["depth"] == 0 and root["role"] in ("orchestrator", "swarm_leader")
        assert root["parent_id"] == rt.commander.id
        kids = [a for a in r1["artifacts"] if a["parent_id"] == root["agent_id"]]
        assert kids and all(a["depth"] == 1 and a["input"] and a["output"] and a["subtask"] for a in kids)
        assert report["totals"]["tokens"] > 0 and report["totals"]["units"] >= 2
        assert sum(r["tokens"] for r in report["rounds"]) == report["totals"]["tokens"]
        assert report["team"] and report["region"]["kind"] == "domain"

        md = await http.get(f"{base}/api/topics/{topic_id}/report.md")
        assert md.status_code == 200 and "attachment" in md.headers["content-disposition"]
        assert md.text.startswith("# Mission Report: Offsite") and "## Round 2 – Feedback" in md.text
        assert (await http.get(f"{base}/api/topics/hq/report")).status_code == 404


async def _wait_done(rt, topic_id, timeout=20):
    for _ in range(int(timeout / 0.05)):
        t = rt.topics.get(topic_id)
        if t and t.status != "active" and t.root_agent_id:
            return t
        await asyncio.sleep(0.05)
    raise AssertionError("mission did not finish")


async def test_mission_with_context_via_rest(server):
    app, base = server
    rt, root = app.state.runtime, app.state.ctx_root
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.get(f"{base}/api/context/inspect", params={"path": str(root)})
        assert r.json()["ok"] and r.json()["kind"] == "dir" and r.json()["files"] == 2
        r = await http.get(f"{base}/api/context/inspect", params={"path": "/etc"})
        assert not r.json()["ok"] and "outside" in r.json()["error"]
        body = {"goal": "Review the project", "title": "Code review", "mode": "hierarchy", "context": [
            {"kind": "file", "name": "brief.md", "content": "Focus on naming."},
            {"kind": "path", "path": str(root)},
            {"kind": "path", "path": "/etc/hosts"},
            {"kind": "note", "name": "constraints", "content": "No new dependencies."}]}
        topic_id = (await http.post(f"{base}/api/missions", json=body)).json()["id"]
        topic = await _wait_done(rt, topic_id)
        assert topic.status == "completed"
        ok = {c["kind"]: c for c in topic.context if c["ok"]}
        assert {"file", "path", "note"} <= set(ok) and "folder" in ok["path"]["detail"]
        assert any(not c["ok"] and "outside" in c["error"] for c in topic.context)
        report = (await http.get(f"{base}/api/topics/{topic_id}/report")).json()
        calls = [c for r_ in report["rounds"] for a in r_["artifacts"] for c in a["tool_calls"]]
        assert {"list_dir", "read_intel"} <= {c["name"] for c in calls}  # mock mode exercises local tools
        assert all("SUPER_SECRET" not in c["result"] for c in calls)
        md = (await http.get(f"{base}/api/topics/{topic_id}/report.md")).text
        assert "## Intel (attached context)" in md and "**Tool calls**" in md


async def test_context_via_native_a2a_parts(server):
    app, _ = server
    rt, root = app.state.runtime, app.state.ctx_root
    from agent_landscape.context.intel import ContextItem, items_to_parts
    parts = items_to_parts("Summarize the repo", [ContextItem(kind="file", name="a.txt", content="alpha"),
                                                   ContextItem(kind="path", path=str(root / "src"))])
    async with httpx.AsyncClient(timeout=60) as http:
        res = await A2AClient(rt.commander.url, http).send(Message(role="user", parts=parts,
                                                                   metadata={"mode": "hierarchy"}))
    assert res.status.state == TaskState.completed
    intel = rt.intel[res.context_id]
    assert "a.txt" in intel.docs and intel.roots == [(root / "src").resolve()]
    assert rt.topics[res.context_id].goal == "Summarize the repo"


async def test_live_tool_loop_with_stub_llm(server):
    """Non-mock path: the model requests tools, the agent executes them and feeds results back."""
    app, base = server
    rt, root = app.state.runtime, app.state.ctx_root
    from agent_landscape.llm import LLMResult, ToolCall
    seen: list[str] = []

    async def fake_complete(messages, *, tools=None, **_):
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        if tools and not tool_msgs:
            return LLMResult(text="", tokens=10, tool_calls=[
                ToolCall(id="c1", name="search_files", arguments='{"query": "magic_marker"}'),
                ToolCall(id="c2", name="read_file", arguments='{"path": ".env"}')])
        if tool_msgs:
            seen.extend(m["content"] for m in tool_msgs)
            return LLMResult(text="Found: " + tool_msgs[0]["content"][:80], tokens=10)
        return LLMResult(text="ok", tokens=5)

    original, rt.settings.mock = rt.llm.complete, False
    rt.llm.complete = fake_complete
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            body = {"goal": "Where is MAGIC_MARKER defined?", "mode": "hierarchy",
                    "context": [{"kind": "path", "path": str(root)}]}
            topic_id = (await http.post(f"{base}/api/missions", json=body)).json()["id"]
            topic = await _wait_done(rt, topic_id)
            report = (await http.get(f"{base}/api/topics/{topic_id}/report")).json()
    finally:
        rt.llm.complete, rt.settings.mock = original, True
    assert topic.status == "completed", [(a.name, a.status, a.activity) for a in rt.agents.values()
                                         if a.topic_id == topic_id]
    assert any("src/tool.py:1" in s for s in seen)
    assert not any("SUPER_SECRET" in s for s in seen) and any("hidden" in s for s in seen)
    calls = [c for r_ in report["rounds"] for a in r_["artifacts"] for c in a["tool_calls"]]
    assert {"search_files", "read_file"} <= {c["name"] for c in calls}
    assert any(not c["ok"] for c in calls if c["name"] == "read_file")


async def test_knowledge_bases_end_to_end(server):
    """Orchestrator creates a mission KB, workers search it, the result is archived into the region
    knowledge base, and the next mission in that region gets it linked automatically."""
    app, base = server
    rt = app.state.runtime
    async with httpx.AsyncClient(timeout=60) as http:
        client = A2AClient(rt.commander.url, http)
        res = await client.send(Message.of("Design a data platform for sensor analytics",
                                           metadata={"title": "Sensor platform", "mode": "hierarchy"}))
        t1 = rt.topics[res.context_id]
        region = rt.regions[t1.region_id]
        own = [k for k in t1.knowledge if k["how"] == "created"]
        assert own and own[0]["name"].startswith("al-") and own[0]["created_by"].startswith("Orchestrator")
        report = (await http.get(f"{base}/api/topics/{t1.id}/report")).json()
        calls = [(a["role"], c["name"]) for r_ in report["rounds"] for a in r_["artifacts"] for c in a["tool_calls"]]
        assert ("orchestrator", "kb_create") in calls and ("worker", "kb_search") in calls
        assert ("worker", "kb_create") not in calls

        archive_name = rt.vectors.region_store_name(region)
        for _ in range(100):  # archive runs in the background
            if any(s.name == archive_name and s.files for s in await rt.vectors.stores(refresh=True)):
                break
            await asyncio.sleep(0.05)
        res2 = await client.send(Message.of("Sensor platform: add alerting", metadata={
            "title": "Sensor alerting", "mode": "hierarchy", "region_id": region.id}))
        t2 = rt.topics[res2.context_id]
        assert any(k["name"] == archive_name and k["how"] == "region" for k in t2.knowledge)

        kb = (await http.get(f"{base}/api/knowledge")).json()
        assert kb["enabled"] and kb["kind"] == "memory"
        names = {s["name"]: s for s in kb["stores"]}
        assert archive_name in names and "Sensor alerting" in names[archive_name]["missions"]

        # a user can attach an existing knowledge base in the composer
        body = {"goal": "Summarize what we know", "mode": "hierarchy", "region_id": region.id,
                "context": [{"kind": "store", "store": own[0]["name"]}, {"kind": "store", "store": "nope"}]}
        tid = (await http.post(f"{base}/api/missions", json=body)).json()["id"]
        t3 = await _wait_done(rt, tid)
        assert any(k["name"] == own[0]["name"] and k["how"] == "attached" for k in t3.knowledge)
        assert any(not c["ok"] and "not found" in c["error"] for c in t3.context)


async def test_jsonrpc_errors(server):
    app, base = server
    url = app.state.runtime.commander.url
    async with httpx.AsyncClient() as http:
        r = (await http.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "nope"})).json()
        assert r["error"]["code"] == -32601
        r = (await http.post(url, json={"jsonrpc": "2.0", "id": 2, "method": "tasks/get",
                                        "params": {"id": "missing"}})).json()
        assert r["error"]["code"] == -32001
        r = (await http.post(url, json={"jsonrpc": "2.0", "id": 3, "method": "message/send",
                                        "params": {"message": {"role": "bogus"}}})).json()
        assert r["error"]["code"] == -32602

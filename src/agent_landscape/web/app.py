"""FastAPI app: A2A endpoints, REST API for the UI, websocket live feed, static UI."""

from __future__ import annotations

import asyncio
import contextlib
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..a2a.server import create_a2a_router
from ..agents.profiles import KINDS, TOOL_FAMILIES
from ..config import Settings
from ..context import sandbox
from ..context.intel import ContextItem
from ..runtime.reports import build_report, report_markdown
from ..runtime.runtime import Runtime

STATIC_DIR = Path(__file__).parent / "static"


class MissionRequest(BaseModel):
    goal: str = Field(min_length=3, max_length=8000)
    title: str | None = Field(default=None, max_length=80)
    mode: str = "auto"  # auto | hierarchy | swarm
    swarm_size: int | None = Field(default=None, ge=2, le=12)
    model: str | None = None
    region_id: str | None = None  # None → the Command HQ decides the region
    profile: str | None = None  # orchestrator / hive-leader profile for the mission's root agent
    context: list[ContextItem] = Field(default_factory=list, max_length=50)


class ProfileIn(BaseModel):
    key: str = Field(min_length=1, max_length=48)
    kind: str
    title: str = Field(min_length=1, max_length=40)
    icon: str = Field(default="🤖", max_length=8)
    description: str = Field(default="", max_length=400)
    prompt: str = Field(min_length=1, max_length=20000)
    model: str | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    tools: list[str] = Field(default_factory=list)
    grant_mcp: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=lambda: ["*"])
    prompts: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True


class FeedbackRequest(BaseModel):
    text: str = Field(min_length=2, max_length=8000)
    context: list[ContextItem] = Field(default_factory=list, max_length=50)


def _check_context_size(items: list[ContextItem]) -> None:
    total = sum(len(i.content or "") for i in items)
    if total > 5_000_000:
        raise HTTPException(413, f"Attachments too large ({total / 1e6:.1f} MB, max 5 MB)")


class RegionUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=40)
    biome: str | None = None


class RemoteAgentRequest(BaseModel):
    url: str
    api_key: str | None = None
    headers: dict[str, str] | None = None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    runtime = Runtime(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await runtime.start()
        yield
        await runtime.stop()

    app = FastAPI(title="Agent Landscape", version="0.1.0", lifespan=lifespan)
    app.state.runtime = runtime
    app.include_router(create_a2a_router(runtime.get_agent, runtime.tasks))

    # Root agent card → the Commander (register this URL in LiteLLM's Agent Gateway)
    @app.get("/.well-known/agent-card.json", include_in_schema=False)
    @app.get("/.well-known/agent.json", include_in_schema=False)
    async def root_card():
        assert runtime.commander is not None
        return JSONResponse(runtime.commander.card().dump())

    # ---------------------------------------------------------------- REST
    @app.get("/api/state")
    async def state():
        return runtime.snapshot()

    @app.get("/api/models")
    async def models():
        return {"models": await runtime.llm.list_models(), "default": settings.default_model}

    @app.post("/api/missions")
    async def create_mission(req: MissionRequest):
        _check_context_size(req.context)
        topic = await runtime.start_mission(req.goal, title=req.title, mode=req.mode,
                                            swarm_size=req.swarm_size, model=req.model or None,
                                            region_id=req.region_id or None, context=req.context,
                                            profile=req.profile or None)
        return topic.to_dict()

    # ------------------------------------------------------- profiles
    @app.get("/api/profiles")
    async def list_profiles():
        reg = runtime.profiles
        reg.refresh()
        return {"profiles": [p.to_dict() for p in sorted(reg.profiles.values(), key=lambda p: (p.kind, p.key))],
                "builtin": sorted(reg.builtin), "kinds": list(KINDS), "families": TOOL_FAMILIES,
                "editable": reg.user_dir is not None, "dir": str(reg.user_dir) if reg.user_dir else None,
                "errors": reg.errors}

    @app.put("/api/profiles/{key}")
    async def save_profile(key: str, req: ProfileIn):
        if req.key != key:
            raise HTTPException(400, "Key in the URL and body differ")
        try:
            prof = runtime.profiles.save(req.model_dump())
        except (ValueError, PermissionError) as exc:
            raise HTTPException(400, str(exc)) from exc
        runtime.log(f"⚙️ Profile '{prof.title}' ({prof.key}) saved → {prof.path}", level="success")
        runtime.emit("profiles_changed")
        return prof.to_dict()

    @app.delete("/api/profiles/{key}")
    async def delete_profile(key: str):
        try:
            result = runtime.profiles.remove(key)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        runtime.emit("profiles_changed")
        return {"ok": True, "result": result, "profile": (runtime.profiles.get(key).to_dict()
                                                           if runtime.profiles.get(key) else None)}

    # --------------------------------------------------------- skills
    @app.get("/api/skills")
    async def list_skills():
        reg = runtime.skills
        reg.refresh()
        return {"skills": [s.brief() for s in reg.skills.values()], "roots": [str(r) for r in reg.roots],
                "errors": reg.errors}

    @app.get("/api/skills/{name}")
    async def get_skill(name: str):
        skill = runtime.skills.get(name)
        if skill is None:
            raise HTTPException(404, "Unknown skill")
        return {**skill.brief(), "instructions": skill.instructions()}

    @app.get("/api/context/inspect")
    async def inspect_path(path: str):
        """Validate a folder/file path for the composer (sandbox rules apply)."""
        try:
            p = sandbox.check_allowed(path, settings.context_roots)
            return {"ok": True, **sandbox.inspect(p)}
        except sandbox.SandboxError as exc:
            return {"ok": False, "error": str(exc)}

    @app.get("/api/knowledge")
    async def list_knowledge(refresh: bool = False):
        """Knowledge bases (vector stores) agents can use, with the missions they are linked to."""
        v = runtime.vectors
        if not v.enabled:
            return {"enabled": False, "kind": v.kind, "stores": []}
        try:
            stores = await v.stores(refresh=refresh)
        except Exception as exc:  # noqa: BLE001
            return {"enabled": True, "kind": v.kind, "error": str(exc), "stores": []}
        missions: dict[str, list[str]] = {}
        for tid, links in v.links.items():
            t = runtime.topics.get(tid)
            for sid in links:
                if t is not None:
                    missions.setdefault(sid, []).append(t.title)
        out = [{**s.brief(), "missions": missions.get(s.id, []), "used_by": sorted(v.usage.get(s.id, set()))}
               for s in stores]
        out.sort(key=lambda s: (not s["owned"], s["name"]))
        return {"enabled": True, "kind": v.kind, "prefix": v.prefix, "stores": out}

    @app.get("/api/tools")
    async def list_tools(refresh: bool = False):
        """Tools agents can be given: built-in sandbox tools + MCP tools from the LiteLLM gateway."""
        tools, status = await runtime.mcp.catalog(refresh=refresh)
        servers: dict[str, dict] = {}
        for name, st in status.items():
            if not name.startswith("_"):
                servers[name] = {"name": name, "status": (st or {}).get("status", "?"), "tools": []}
        for t in tools:
            servers.setdefault(t.server, {"name": t.server, "status": "ok", "tools": []})["tools"].append(t.brief())
        return {"available": runtime.mcp.available, "error": status.get("_error"),
                "mock": settings.mock, "servers": sorted(servers.values(), key=lambda s: s["name"])}

    @app.post("/api/topics/{topic_id}/feedback")
    async def topic_feedback(topic_id: str, req: FeedbackRequest):
        _check_context_size(req.context)
        topic = await runtime.send_followup(topic_id, req.text, context=req.context)
        if topic is None:
            raise HTTPException(409, "Feedback is possible once a mission has finished")
        return topic.to_dict()

    def _report(topic_id: str) -> dict:
        topic = runtime.topics.get(topic_id)
        if topic is None or topic.kind != "mission":
            raise HTTPException(404, "Unknown mission")
        return build_report(runtime, topic)

    @app.get("/api/topics/{topic_id}/report")
    async def topic_report(topic_id: str):
        return _report(topic_id)

    @app.get("/api/topics/{topic_id}/report.md")
    async def topic_report_md(topic_id: str):
        report = _report(topic_id)
        slug = re.sub(r"[^a-z0-9]+", "-", report["topic"]["title"].lower()).strip("-") or "mission"
        return PlainTextResponse(report_markdown(report), media_type="text/markdown; charset=utf-8",
                                 headers={"Content-Disposition": f'attachment; filename="mission-{slug}.md"'})

    @app.post("/api/topics/{topic_id}/resume")
    async def resume_topic(topic_id: str):
        topic = await runtime.resume_topic(topic_id)
        if topic is None:
            raise HTTPException(409, "Only interrupted missions can be resumed")
        return topic.to_dict()

    @app.get("/api/search")
    async def search(q: str, limit: int = 20):
        """Full-text search over all mission deliverables and agent artifacts (SQLite FTS5)."""
        hits = runtime.persist.search(q, max(1, min(limit, 50)))
        if hits is None:
            return {"enabled": False, "hits": []}
        for h in hits:
            t = runtime.topics.get(h["topic_id"])
            h["mission"] = t.title if t else None
            h["exists"] = t is not None
        return {"enabled": True, "hits": hits}

    @app.patch("/api/regions/{region_id}")
    async def patch_region(region_id: str, req: RegionUpdate):
        region = runtime.update_region(region_id, name=req.name, biome=req.biome)
        if region is None:
            raise HTTPException(404, "Unknown region (the HQ region can't be changed)")
        return region.to_dict()

    @app.post("/api/topics/{topic_id}/cancel")
    async def cancel_topic(topic_id: str):
        if not await runtime.cancel_topic(topic_id):
            raise HTTPException(409, "Topic is not running")
        return {"ok": True}

    @app.delete("/api/topics/{topic_id}")
    async def delete_topic(topic_id: str):
        if not runtime.remove_topic(topic_id):
            raise HTTPException(409, "Only finished missions can be dismissed")
        return {"ok": True}

    @app.get("/api/agents/{agent_id}")
    async def get_agent(agent_id: str):
        agent = runtime.get_agent(agent_id)
        if agent is None:
            raise HTTPException(404, "Unknown agent")
        return {**agent.to_dict(), "card": agent.card().dump()}

    @app.post("/api/allies")
    async def add_ally(req: RemoteAgentRequest):
        try:
            agent = await runtime.register_remote(req.url, req.api_key, req.headers)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"Could not register agent: {exc}") from exc
        return agent.to_dict()

    # ----------------------------------------------------------- websocket
    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        q = runtime.bus.subscribe()
        try:
            await websocket.send_json({"type": "snapshot", **runtime.snapshot()})

            async def pump():
                with contextlib.suppress(Exception):
                    while True:
                        await websocket.send_json(await q.get())

            async def drain():
                with contextlib.suppress(Exception):
                    while True:
                        await websocket.receive_text()

            tasks = [asyncio.create_task(pump()), asyncio.create_task(drain())]
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
        except WebSocketDisconnect:
            pass
        finally:
            runtime.bus.unsubscribe(q)
            with contextlib.suppress(Exception):
                await websocket.close()

    # -------------------------------------------------------------- static
    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.middleware("http")
    async def no_stale_ui(request, call_next):
        # UI files change with every update – make browsers revalidate (cheap 304 via ETag)
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app

"""FastAPI app: A2A endpoints, REST API for the UI, websocket live feed, static UI."""

from __future__ import annotations

import asyncio
import contextlib
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..a2a.server import create_a2a_router
from ..agents.profiles import KINDS, TOOL_FAMILIES
from ..config import Settings
from ..context import documents
from ..context import repos as gitrepo
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
    proposal_id: str | None = None  # approved operations proposal this mission comes from
    new_region: dict[str, str] | None = None  # chart this region first (proposal suggested a new one)


class TeamRunRequest(BaseModel):
    prompt: str | None = Field(default=None, max_length=8000)


class TeamPauseRequest(BaseModel):
    paused: bool


class TeamImplementRequest(BaseModel):
    findings: str = Field(min_length=1, max_length=400)  # e.g. "F1, F3"
    notes: str | None = Field(default=None, max_length=4000)


class SimulateRequest(BaseModel):
    channel: str = "outlook"
    relevant: bool | None = None  # None = random pool message
    sender: str | None = Field(default=None, max_length=200)
    subject: str | None = Field(default=None, max_length=300)
    body: str | None = Field(default=None, max_length=8000)


class ProfileIn(BaseModel):
    key: str = Field(min_length=1, max_length=48)
    kind: str
    title: str = Field(min_length=1, max_length=40)
    icon: str = Field(default="🤖", max_length=8)
    description: str = Field(default="", max_length=400)
    prompt: str = Field(min_length=1, max_length=20000)
    model: str | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tool_steps: int | None = Field(default=None, ge=1, le=40)
    tools: list[str] = Field(default_factory=list)
    grant_mcp: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=lambda: ["*"])
    prompts: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True


class AgentPatch(BaseModel):
    pinned_model: str | None = Field(default=None, max_length=200)  # None / "" = unpin


class ConfigPatch(BaseModel):
    default_model: str = Field(min_length=1, max_length=200)  # runtime only – .env stays untouched


class RepoIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    url: str | None = Field(default=None, max_length=500)
    path: str | None = Field(default=None, max_length=1000)
    branch: str | None = Field(default=None, max_length=200)
    description: str = Field(default="", max_length=400)
    keywords: list[str] = Field(default_factory=list, max_length=20)


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

    @app.patch("/api/config")
    async def patch_config(req: ConfigPatch):
        """Switch DEFAULT_MODEL until the next restart (HUD model chip)."""
        model = req.default_model.strip()
        if not settings.mock:
            known = await runtime.llm.list_models()
            if known and model not in known:
                raise HTTPException(400, f"Unknown model '{model}' – pick one from /api/models")
        settings.default_model = model
        runtime.model_problems = await runtime.llm.check_models()
        config = runtime.snapshot()["config"]
        runtime.emit("config_changed", config=config)
        runtime.log(f"🎛️ Default model switched to {model} (until restart)")
        return config

    @app.post("/api/missions")
    async def create_mission(req: MissionRequest):
        _check_context_size(req.context)
        topic = await runtime.start_mission(req.goal, title=req.title, mode=req.mode,
                                            swarm_size=req.swarm_size, model=req.model or None,
                                            region_id=req.region_id or None, context=req.context,
                                            profile=req.profile or None, new_region=req.new_region or None,
                                            proposal_id=req.proposal_id or None)
        return topic.to_dict()

    # ------------------------------------------------------------ teams
    @app.get("/api/teams")
    async def teams_state():
        return runtime.teams.snapshot()

    @app.post("/api/teams/{key}/run")
    async def team_run(key: str, req: TeamRunRequest):
        try:
            topic = await runtime.teams.run(key, prompt=req.prompt)
        except KeyError:
            raise HTTPException(404, "Unknown team") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return topic.to_dict()

    @app.post("/api/teams/{key}/pause")
    async def team_pause(key: str, req: TeamPauseRequest):
        try:
            runtime.teams.set_paused(key, req.paused)
        except KeyError:
            raise HTTPException(404, "Unknown team") from None
        return {"ok": True, "paused": req.paused}

    @app.post("/api/teams/{key}/runs/{topic_id}/implement")
    async def team_implement(key: str, topic_id: str, req: TeamImplementRequest):
        """Approve findings of a team run: feedback round on the same mission, repos upgraded to editable."""
        team = runtime.teams.get(key)
        topic = runtime.topics.get(topic_id)
        if team is None or topic is None or topic.team != key:
            raise HTTPException(404, "Unknown team run")
        text = (f"Approved by the human – implement these findings now: {req.findings.strip()}.\n"
                "Give each approved finding to the integrator (✍️ 🧪) as its own work item with the exact files, "
                "the change and the test to run. Skip everything that was not approved. Report per finding: "
                "done / skipped, commit and test result."
                + (f"\n\nNotes from the human: {req.notes.strip()}" if req.notes and req.notes.strip() else ""))
        items = [ContextItem(kind="repo", repo=r["repo"], write=True, ref=r.get("ref")) for r in team.repos]
        res = await runtime.send_followup(topic_id, text, context=items)
        if res is None:
            raise HTTPException(409, "The run is still active – wait until it has finished")
        runtime.log(f"✅ You approved findings {req.findings.strip()} for '{topic.title}'", topic_id=topic_id,
                    level="success")
        return res.to_dict()

    # ------------------------------------------------- operations team
    @app.get("/api/ops")
    async def ops_state():
        return runtime.ops.snapshot()

    @app.post("/api/ops/refresh")
    async def ops_refresh():
        """Manual refresh: check Outlook / Teams sources now instead of waiting for the next poll."""
        try:
            return {"channels": await runtime.ops.poll_now()}
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/ops/simulate")
    async def ops_simulate(req: SimulateRequest):
        """Mock source: a message arrives in Outlook / Teams right now (the monitor picks it up)."""
        try:
            return await runtime.ops.simulate(req.channel, relevant=req.relevant, sender=req.sender,
                                              subject=req.subject, body=req.body)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/ops/proposals/{proposal_id}/dismiss")
    async def ops_dismiss(proposal_id: str):
        p = runtime.ops.dismiss(proposal_id)
        if p is None:
            raise HTTPException(409, "Only pending proposals can be dismissed")
        return p.to_dict()

    @app.post("/api/ops/proposals/{proposal_id}/feedback")
    async def ops_approve_feedback(proposal_id: str, req: FeedbackRequest):
        """Approve a 'feedback round' proposal: the existing mission team re-works its deliverable."""
        p = runtime.ops.proposals.get(proposal_id)
        if p is None or p.status != "pending" or not p.followup_topic_id:
            raise HTTPException(409, "Not a pending feedback proposal")
        _check_context_size(req.context)
        topic = await runtime.send_followup(p.followup_topic_id, req.text, context=req.context)
        if topic is None:
            raise HTTPException(409, "The mission is still running or can't take feedback right now")
        runtime.ops.approved(p.id, topic.id)
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

    # ---------------------------------------------------------- repos
    @app.get("/api/repos")
    async def list_repos():
        reg = runtime.repos
        reg.refresh()
        specs = sorted(reg.repos.values(), key=lambda s: s.name)
        rows = await asyncio.gather(*(reg.status(s) for s in specs), return_exceptions=True)
        out = [r if isinstance(r, dict) else {**s.to_dict(), "error": str(r)} for s, r in zip(specs, rows)]
        return {"repos": out, "file": str(reg.file) if reg.file else None, "editable": reg.file is not None,
                "workspace": str(reg.workspace), "errors": reg.errors}

    @app.put("/api/repos/{name}")
    async def save_repo(name: str, req: RepoIn):
        if req.name.strip().lower() != name.lower():
            raise HTTPException(400, "Name in the URL and body differ")
        try:
            spec = await runtime.repos.save(req.model_dump())
        except (ValueError, PermissionError) as exc:  # RepoError / SandboxError are ValueErrors
            raise HTTPException(400, str(exc)) from exc
        runtime.log(f"📦 Repo '{spec.name}' registered ({spec.path or spec.url})", level="success")
        warning = None
        try:  # clone / fetch right away: validates URL + credentials and makes the first mission fast
            _, warning = await runtime.repos.ensure_checkout(spec)
        except ValueError as exc:
            warning = str(exc)
        return {**await runtime.repos.status(spec), "warning": warning}

    @app.delete("/api/repos/{name}")
    async def delete_repo(name: str):
        if not runtime.repos.remove(name):
            raise HTTPException(404, "Unknown repo")
        return {"ok": True}  # clones in the workspace are kept (cheap re-registration)

    @app.post("/api/repos/{name}/sync")
    async def sync_repo(name: str):
        spec = runtime.repos.get(name)
        if spec is None:
            raise HTTPException(404, "Unknown repo")
        try:
            _, warning = await runtime.repos.ensure_checkout(spec)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {**await runtime.repos.status(spec), "warning": warning}

    def _mission_repo(topic_id: str, name: str):
        intel = runtime.intel.get(topic_id)
        mr = intel.repo(name) if intel else None
        if mr is None:
            raise HTTPException(404, "Repo is not attached to this mission")
        return mr

    @app.get("/api/topics/{topic_id}/repos")
    async def topic_repos(topic_id: str):
        """Code changes of a mission: per repo branch, commits, diff (for review before pushing)."""
        intel = runtime.intel.get(topic_id)
        out = []
        for mr in (intel.repos if intel else []):
            entry = {**mr.to_dict(), "exists": Path(mr.path).exists()}
            if entry["exists"]:
                try:
                    await gitrepo.refresh_changes(mr)
                    entry["changed"] = mr.changed
                    entry["stat"] = await gitrepo.diff(mr, stat=True)
                    entry["diff"] = await gitrepo.diff(mr)
                    entry["pr_host"] = (gitrepo.pr_target(await gitrepo.remote_url(mr)) or {}).get("host")
                except ValueError as exc:
                    entry["error"] = str(exc)
            out.append(entry)
        return {"repos": out}

    @app.post("/api/topics/{topic_id}/repos/{name}/push")
    async def push_repo(topic_id: str, name: str):
        """Human approval step: push the mission branch to origin (agents never push)."""
        mr = _mission_repo(topic_id, name)
        try:
            out = await gitrepo.push(mr)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        runtime.log(f"📦 Pushed {mr.branch} of {mr.name} to origin", topic_id=topic_id, level="success")
        runtime.repo_changed(topic_id, mr, by="you")
        return {"ok": True, "output": out, "pushed": mr.pushed}

    def _pr_body(topic, mr) -> str:
        """Default PR description: the mission deliverable + commit list (the human can edit it before sending)."""
        commits = "\n".join(f"- `{c['sha']}` {c['message']} ({c['author']})" for c in mr.commits)
        result = (topic.result or "").strip() if topic else ""
        if len(result) > 20_000:
            result = result[:20_000] + "\n\n… (truncated – full report in Agent Landscape)"
        parts = [f"## Mission\n{topic.title}" if topic else "", f"## Summary\n{result}" if result else "",
                 f"## Commits\n{commits}" if commits else "",
                 "---\n_Opened from Agent Landscape. Commits were written by agents on a mission branch and reviewed "
                 "by a human before this PR._"]
        return "\n\n".join(p for p in parts if p)

    @app.get("/api/topics/{topic_id}/repos/{name}/pr")
    async def pr_draft(topic_id: str, name: str):
        """Prefilled title/body for the PR dialog."""
        mr = _mission_repo(topic_id, name)
        topic = runtime.topics.get(topic_id)
        return {"title": topic.title if topic else mr.branch, "body": _pr_body(topic, mr),
                "base": gitrepo.base_branch(mr), "branch": mr.branch, "pr": mr.pr,
                "host": (gitrepo.pr_target(await gitrepo.remote_url(mr)) or {}).get("host")}

    @app.post("/api/topics/{topic_id}/repos/{name}/pr")
    async def create_pr(topic_id: str, name: str, body: dict | None = None):
        """Human approval step: push (if needed) + open a pull request with the user's gh / az login."""
        mr = _mission_repo(topic_id, name)
        topic = runtime.topics.get(topic_id)
        body = body or {}
        try:
            pr = await gitrepo.create_pr(mr, str(body.get("title") or (topic.title if topic else "")),
                                         str(body.get("body") or _pr_body(topic, mr)), draft=bool(body.get("draft")))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        verb = "Found open" if pr.get("existing") else "Opened"
        runtime.log(f"🔀 {verb} pull request {pr['url']} for {mr.name}", topic_id=topic_id, level="success")
        runtime.repo_changed(topic_id, mr, by="you")
        return {"ok": True, "pr": pr, "pushed": mr.pushed}

    @app.post("/api/context/convert")
    async def convert_document(request: Request, name: str):
        """PDF / Word / PowerPoint / Excel (raw request body) → markdown text for the briefing (local, offline)."""
        data = await request.body()
        try:
            text, kind = await asyncio.to_thread(documents.convert, name, data)
        except documents.ConversionError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"name": name, "kind": kind, "bytes": len(data), "chars": len(text), "text": text}

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

    @app.post("/api/knowledge/import-archive")
    async def import_archive():
        """(Re-)import exported mission reports from MISSION_ARCHIVE_DIR into the region knowledge bases."""
        if not runtime.vectors.enabled:
            raise HTTPException(400, "Knowledge bases are disabled (VECTOR_STORE_URL not set)")
        if not settings.mission_archive_dir:
            raise HTTPException(400, "MISSION_ARCHIVE_DIR is off")
        return await runtime.import_mission_archive()

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

    @app.patch("/api/agents/{agent_id}")
    async def patch_agent(agent_id: str, req: AgentPatch):
        agent = runtime.get_agent(agent_id)
        if agent is None:
            raise HTTPException(404, "Unknown agent")
        if agent.role == "ally":
            raise HTTPException(400, "Allied agents run their own model")
        model = (req.pinned_model or "").strip() or None
        if model and not settings.mock:
            known = await runtime.llm.list_models()
            if known and model not in known:
                raise HTTPException(400, f"Unknown model '{model}' – pick one from /api/models")
        agent.pinned_model = model
        runtime.agent_changed(agent)
        runtime.log(f"🎛️ {agent.name}: model {'pinned to ' + model if model else 'unpinned (inherits again)'}",
                    agent=agent)
        return agent.to_dict()

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

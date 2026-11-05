"""Runtime: agent registry, topics (territories), placement on the hex map, telemetry."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

from ..a2a.client import A2AClient
from ..a2a.task_manager import TaskManager
from ..a2a.types import Message
from ..agents.base import ACTIVE_STATES, Agent, short
from ..agents.profiles import ProfileRegistry
from ..agents.roles import CALLSIGNS
from ..context.skills import SkillRegistry
from ..agents.units import Commander, RemoteAgent
from ..config import Settings
from ..llm import LLM
from . import hexgrid
from .events import EventBus
from ..context.intel import ContextItem, MissionIntel, items_to_parts
from ..context.intel import resolve as resolve_intel
from ..context.mcp import MCPGateway
from ..context.repos import MissionRepo, RepoError, RepoRegistry
from ..context.repos import commit as repo_commit
from ..context.repos import refresh_changes as repo_refresh
from ..context.toolbox import Toolbox
from ..context.vectorstore import HTTPBackend, MemoryBackend, StoreInfo, VectorService, parse_report, slug
from ..ops.service import OpsService
from ..storage.db import Database
from ..teams.service import TeamService
from .persistence import NullPersistence, Persistence
from .regions import BIOMES, DOMAIN_BIOMES, Region, guess_biome
from .reports import build_report, report_markdown

log = logging.getLogger(__name__)

TOPIC_COLORS = ["#4fc3f7", "#ff8a65", "#aed581", "#ba68c8", "#ffd54f", "#4db6ac", "#f06292",
                "#7986cb", "#e57373", "#81c784", "#64b5f6", "#ffb74d"]
HQ_TOPIC_ID = "hq"
HQ_REGION_ID = "hq-region"
ALLIES_TOPIC_ID = "allies"
# finished units that can be recruited again ("idle" = freshly spawned, about to get its first order)
REUSABLE_STATES = {"done", "canceled", "failed"}


@dataclass
class Topic:
    id: str
    title: str
    goal: str
    color: str
    center: tuple[int, int] | None = None  # None until the HQ has surveyed it into a region
    region_id: str | None = None
    mode: str = "auto"
    status: str = "active"  # active | completed | failed | canceled | base
    kind: str = "mission"  # mission | hq | allies
    root_agent_id: str | None = None
    task_id: str | None = None
    result: str | None = None
    rounds: int = 1
    feedback: list[str] = field(default_factory=list)
    feedback_queued: bool = False  # sent to the Commander, round not started yet
    tokens: int = 0
    cost: float = 0.0
    context: list[dict[str, Any]] = field(default_factory=list)  # attached intel (summary, incl. errors)
    knowledge: list[dict[str, Any]] = field(default_factory=list)  # linked knowledge bases (vector stores)
    team: str | None = None  # key of the team (teams.yaml) that runs this mission
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "goal": self.goal, "color": self.color,
            "q": self.center[0] if self.center else None, "r": self.center[1] if self.center else None,
            "radius": hexgrid.TERRITORY_RADIUS, "region_id": self.region_id,
            "mode": self.mode, "status": self.status, "kind": self.kind,
            "root_agent_id": self.root_agent_id, "task_id": self.task_id,
            "result": self.result, "rounds": self.rounds, "feedback": self.feedback,
            "tokens": self.tokens, "cost": round(self.cost, 6), "context": self.context,
            "knowledge": self.knowledge, "team": self.team,
            "created_at": self.created_at, "finished_at": self.finished_at,
        }


class Runtime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.llm = LLM(settings)
        self.tasks = TaskManager()
        self.bus = EventBus()
        self.agents: dict[str, Agent] = {}
        self.topics: dict[str, Topic] = {}
        self.regions: dict[str, Region] = {}
        self.occupied: dict[tuple[int, int], str] = {}
        self._territory_index = 0
        self._region_index = 0
        self._callsign = 0
        self._background: set[asyncio.Task] = set()
        self.stats = {"tokens": 0, "cost": 0.0, "a2a_messages": 0, "agents_spawned": 0,
                      "missions_completed": 0, "missions_failed": 0, "peak_active": 0,
                      "veterans_redeployed": 0, "feedback_rounds": 0}
        self.http: httpx.AsyncClient | None = None
        self.commander: Commander | None = None
        self.model_problems: list[str] = []
        self._last_active = 0
        self.mcp = MCPGateway(settings)
        self.intel: dict[str, MissionIntel] = {}
        self._toolboxes: dict[str, Toolbox] = {}
        if settings.mock:  # never touch the real knowledge bases in mock mode
            backend, kind = MemoryBackend(), "memory"
        elif settings.vector_url:
            backend, kind = HTTPBackend(settings.vector_url, settings.vector_key, settings.vector_parser), "http"
        else:
            backend, kind = None, "off"
        self.vectors = VectorService(backend, prefix=settings.vector_prefix,
                                     auto_archive=settings.vector_auto_archive, kind=kind)
        self.archive_imported: set[str] = set()  # mission-archive files already in the vector store (path:hash)
        self.profiles = ProfileRegistry(settings.profiles_dir)
        self.skills = SkillRegistry(settings.skills_dirs)
        self.repos = RepoRegistry(settings.repos_file, settings.workspace_dir, settings.context_roots)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.ops = OpsService(self)
        self.teams = TeamService(self)
        self.persist: NullPersistence = (Persistence(self, Database(settings.db_path)) if settings.db_path
                                         else NullPersistence())
        self.tasks.listener = self.persist.task_id

    # ----------------------------------------------------------- lifecycle
    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=None),
            limits=httpx.Limits(max_connections=1000, max_keepalive_connections=100),
        )
        self.persist.restore()  # regions, missions, agents, tasks, intel … from SQLite (if enabled)
        hq_region = self.regions.get(HQ_REGION_ID) or self._create_region(
            "Command HQ", "Headquarters of the landscape", "capital", kind="hq", region_id=HQ_REGION_ID)
        hq = self.topics.get(HQ_TOPIC_ID)
        if hq is None:
            hq = self._new_topic("Command HQ", "Entry point for all missions", kind="hq", topic_id=HQ_TOPIC_ID)
            hq.status = "base"
            self.place_topic(hq, hq_region)
        # stable id → stable A2A URL (/a2a/commander) for registration in LiteLLM's Agent Gateway
        restored = self.agents.get("commander")
        self.commander = restored if isinstance(restored, Commander) else self.spawn(  # type: ignore[assignment]
            Commander, topic_id=hq.id, parent=None, name="High Commander", depth=0, agent_id="commander")
        self.ops.start()  # always-on operations team (read-only monitors) – not started by the Commander
        self.persist.start()
        self.teams.start()  # scheduled team runs (teams.yaml)
        if self.settings.mission_archive_dir and self.vectors.enabled:
            self._spawn_background(self.import_mission_archive())
        self.model_problems = await self.llm.check_models()
        for problem in self.model_problems:
            log.warning(problem)

    def prepare_shutdown(self) -> None:
        """Persist the current state *before* shutdown cancels running work (called on SIGINT/SIGTERM),
        so running missions restart as 'interrupted' – not as failed/canceled."""
        self.persist.freeze()

    async def stop(self) -> None:
        await self.teams.stop()
        await self.ops.stop()
        await self.persist.stop()
        for t in list(self._background):
            t.cancel()
        for runner in list(self.tasks.runners.values()):
            runner.cancel()
        if self.http:
            await self.http.aclose()
        await self.vectors.close()

    # ------------------------------------------------------------- topics
    def _new_topic(self, title: str, goal: str, *, kind: str = "mission", mode: str = "auto",
                   topic_id: str | None = None) -> Topic:
        idx = self._territory_index
        self._territory_index += 1
        topic = Topic(
            id=topic_id or f"topic-{uuid.uuid4().hex[:8]}",
            title=title, goal=goal, kind=kind, mode=mode,
            color="#f5c542" if kind == "hq" else "#9e9e9e" if kind == "allies"
            else TOPIC_COLORS[(idx - 1) % len(TOPIC_COLORS)],
        )
        self.topics[topic.id] = topic
        self.persist.topic(topic)
        self.persist.kv()
        self.bus.emit("topic_created", topic=topic.to_dict())
        return topic

    def create_topic(self, title: str, goal: str, mode: str = "auto", topic_id: str | None = None) -> Topic:
        mode = mode if mode in ("auto", "hierarchy", "swarm") else "auto"
        if topic_id in self.topics:
            topic_id = None
        topic = self._new_topic(short(title, 60), goal, mode=mode, topic_id=topic_id)
        self.log(f"New mission: {topic.title} ({mode})", topic_id=topic.id)
        return topic

    def topic_changed(self, topic: Topic) -> None:
        self.persist.topic(topic)
        self.bus.emit("topic_updated", topic=topic.to_dict())

    def reopen_topic(self, topic: Topic, feedback: str) -> None:
        """Start another feedback round of a finished mission (same territory, same team)."""
        topic.rounds += 1
        topic.feedback.append(short(feedback, 1000))
        topic.status, topic.finished_at, topic.feedback_queued = "active", None, False
        self.stats["feedback_rounds"] += 1
        self.topic_changed(topic)
        self._emit_stats()
        self.log(f"Feedback round {topic.rounds} for '{topic.title}'", topic_id=topic.id)

    # -------------------------------------------------------------- intel
    async def attach_intel(self, topic: Topic, items: list[ContextItem]) -> MissionIntel:
        """Resolve attachments (sandboxed) and make them available to every agent of the mission."""
        assert self.http is not None
        cur = self.intel.get(topic.id)

        async def repo_hook(item: ContextItem) -> tuple[MissionRepo, str | None]:
            existing = cur.repo(item.repo) if cur is not None and item.repo else None
            if existing is not None:  # feedback round: same worktree, upgrade to editable if asked
                if item.write and not existing.write:
                    await self.repos.make_writable(existing, topic.title, topic.id)
                return existing, None
            spec = self.repos.get(item.repo)
            if spec is None:
                raise RepoError(f"Unknown repo '{item.repo}' – register it first (＋ Add context → Repositories in the briefing)")
            self.log(f"📦 Checking out {spec.name} for '{topic.title}'…", topic_id=topic.id)
            return await self.repos.create_worktree(spec, topic.id, topic.title, ref=item.ref, write=item.write)

        new = await resolve_intel(items, allowed_roots=self.settings.context_roots, http=self.http, mcp=self.mcp,
                                  vectors=self.vectors, repo_hook=repo_hook)
        for store in new.stores:
            self.vectors.link(topic.id, store)
            self.knowledge_changed(topic.id, store, by="you", how="attached", quiet=True)
        if cur is not None:  # feedback round with extra attachments → merge
            for d in new.docs.values():
                cur.add_doc(d)
            cur.roots += [r for r in new.roots if r not in cur.roots]
            cur.tools += [t for t in new.tools if t.ref not in {x.ref for x in cur.tools}]
            cur.stores += [s for s in new.stores if s.id not in {x.id for x in cur.stores}]
            cur.repos += [r for r in new.repos if r not in cur.repos]
            cur.summary += new.summary
            new = cur
        self.intel[topic.id] = new
        self._toolboxes.pop(topic.id, None)
        topic.context = list(new.summary)
        self.persist.intel(topic.id)
        self.persist.links(topic.id)
        self.topic_changed(topic)
        ok = sum(1 for e in new.summary if e.get("ok"))
        failed = [e for e in new.summary if not e.get("ok")]
        self.log(f"Intel for '{topic.title}': {ok} item(s) attached"
                 + (f", {len(failed)} rejected ({failed[0].get('error')})" if failed else ""),
                 topic_id=topic.id, level="warn" if failed else "info")
        return new

    def toolbox_for(self, topic_id: str) -> Toolbox | None:
        intel = self.intel.get(topic_id)
        tb = self._toolboxes.get(topic_id)
        if tb is None:
            tb = self._toolboxes[topic_id] = Toolbox(intel or MissionIntel(), self.mcp, mock=self.settings.mock,
                                                     vectors=self.vectors, runtime=self, topic_id=topic_id)
        return tb

    # -------------------------------------------------------------- repos
    def repo_changed(self, topic_id: str, mr: MissionRepo, *, by: str | None = None, what: str = "") -> None:
        """An agent changed / committed files in a mission repo → persist + live update."""
        self.persist.intel(topic_id)
        if what.startswith("Committed"):
            self.log(f"📦 {by}: {what}", topic_id=topic_id, level="success")
        self.emit("repo_changed", topic_id=topic_id, repo=mr.name, by=by, commits=len(mr.commits),
                  changed=len(mr.changed))

    async def finalize_repos(self, topic: "Topic") -> None:
        """End of a mission round: commit leftovers on mission branches and summarise them in the log."""
        intel = self.intel.get(topic.id)
        for mr in (intel.repos if intel else []):
            if not mr.write:
                continue
            try:
                await repo_refresh(mr)
                if mr.changed:
                    out = await repo_commit(mr, f"chore: uncommitted changes from mission '{topic.title}'",
                                            "Agent Landscape")
                    if out.startswith("Committed"):
                        self.log(f"📦 {mr.name}: auto-committed leftover changes", topic_id=topic.id)
            except RepoError as exc:
                self.log(f"📦 {mr.name}: {exc}", topic_id=topic.id, level="warn")
            if mr.commits:
                self.log(f"📦 {mr.name}: {len(mr.commits)} commit(s) on {mr.branch} – review & push in the report "
                         "(📦 Code)", topic_id=topic.id, level="success")
            self.repo_changed(topic.id, mr)

    def _drop_worktrees(self, intel: MissionIntel | None) -> None:
        if not intel or not intel.repos:
            return

        async def _go() -> None:
            for mr in intel.repos:
                with contextlib.suppress(Exception):
                    await self.repos.remove_worktree(mr)

        t = asyncio.create_task(_go())
        self._background.add(t)
        t.add_done_callback(self._background.discard)

    # ---------------------------------------------------------- knowledge
    def knowledge_changed(self, topic_id: str, store: StoreInfo, *, by: str | None = None, how: str | None = None,
                          quiet: bool = False) -> None:
        """Keep the mission's list of linked knowledge bases (for UI/report) in sync."""
        topic = self.topics.get(topic_id)
        if topic is None:
            return
        entry = next((k for k in topic.knowledge if k["id"] == store.id), None)
        if entry is None:
            entry = {**store.brief(), "linked_by": by, "how": how}
            topic.knowledge.append(entry)
            if not quiet:
                verb = {"created": "created knowledge base", "linked": "linked knowledge base",
                        "region": "linked region knowledge",
                        "recall": "linked recalled knowledge"}.get(how or "", "linked")
                self.log(f"📚 {by or 'HQ'} {verb} '{store.name}'", topic_id=topic_id, level="success")
        entry.update(files=store.files, bytes=store.bytes,
                     used_by=sorted(self.vectors.usage.get(store.id, set())))
        self.persist.links(topic_id)
        self.persist.kv()
        self.topic_changed(topic)

    async def link_region_knowledge(self, topic: Topic) -> None:
        """Missions automatically get read access to their region's knowledge base (archive of past missions)."""
        if not self.vectors.enabled:
            return
        try:
            store = await self.vectors.region_store(self.regions.get(topic.region_id or ""))
        except Exception as exc:  # noqa: BLE001 – knowledge is optional, never block a mission
            self.log(f"Knowledge service unavailable: {exc}", topic_id=topic.id, level="warn")
            return
        if store is not None and store.files and self.vectors.link(topic.id, store):
            self.knowledge_changed(topic.id, store, by="Command HQ", how="region")

    async def recall_past_missions(self, topic: Topic) -> list[dict[str, Any]]:
        """Semantic search over every region archive for past missions related to a new mission."""
        if not self.vectors.enabled or self.settings.vector_recall <= 0:
            return []
        try:
            hits = await self.vectors.recall(f"{topic.title}\n{topic.goal}", limit=self.settings.vector_recall,
                                             min_score=self.settings.vector_recall_min_score)
        except Exception as exc:  # noqa: BLE001 – recall is optional, never block a mission
            self.log(f"Recall of past missions failed: {exc}", topic_id=topic.id, level="warn")
            return []
        if hits:
            self.log(f"🧠 Command HQ recalled {len(hits)} related past mission document(s) from "
                     f"{len({h['store'].id for h in hits})} archive(s)", topic_id=topic.id)
        return hits

    def link_recalled_knowledge(self, topic: Topic, hits: list[dict[str, Any]]) -> None:
        """Archives with recall hits (possibly from other regions) become searchable for the mission team."""
        for h in hits:
            store: StoreInfo = h["store"]
            if self.vectors.link(topic.id, store):
                self.knowledge_changed(topic.id, store, by="Command HQ", how="recall")

    def schedule_archive(self, topic: Topic) -> None:
        """Archive a completed mission (full mission report) into its region knowledge base (background)."""
        if not (self.vectors.enabled and self.vectors.auto_archive and topic.result):
            return
        try:  # build now (event loop thread) – the tasks of this round are complete
            report: str | None = report_markdown(build_report(self, topic))
        except Exception:  # noqa: BLE001 – fall back to objective + deliverable
            log.exception("Could not build the mission report for the archive")
            report = None

        async def _go() -> None:
            try:
                store = await self.vectors.archive(topic, self.regions.get(topic.region_id or ""), report=report)
                if store is not None:
                    self.log(f"📚 Archived '{topic.title}' into '{store.name}'", topic_id=topic.id, level="success")
            except Exception as exc:  # noqa: BLE001
                self.log(f"Could not archive '{topic.title}': {exc}", topic_id=topic.id, level="warn")

        self._spawn_background(_go())

    def _spawn_background(self, coro: Any) -> None:
        t = asyncio.create_task(coro)
        self._background.add(t)
        t.add_done_callback(self._background.discard)

    async def import_mission_archive(self, root: str | None = None) -> dict[str, Any]:
        """Import exported mission reports (``mission-archive/**/*.md``) into the region knowledge stores, so
        recall and region memory cover them. Regions are matched by the report's ``Region:`` line; files already
        imported (same path + content) are skipped."""
        out: dict[str, Any] = {"imported": 0, "skipped": 0, "unparsed": [], "stores": [], "errors": []}
        base = Path(root or self.settings.mission_archive_dir or "")
        if not self.vectors.enabled or not str(base) or not base.is_dir():
            return out
        persistent = self.vectors.kind == "http"  # the in-memory (mock) store is empty after every restart
        groups: dict[str, tuple[str, list[tuple[str, str, str]]]] = {}
        for f in sorted(base.rglob("*.md")):
            try:
                text = f.read_text(errors="replace")
            except OSError as exc:
                out["errors"].append(f"{f.name}: {exc}")
                continue
            _title, region_name = parse_report(text)
            if not region_name:
                out["unparsed"].append(str(f.relative_to(base)))
                continue
            rel = f.relative_to(base)
            key = f"{rel}:{hashlib.sha1(text.encode()).hexdigest()[:12]}"
            if persistent and key in self.archive_imported:
                out["skipped"] += 1
                continue
            fname = f"archive-{'-'.join(slug(p, 30) for p in rel.parent.parts)}-{slug(f.stem, 50)}.md".replace("--", "-")
            groups.setdefault(region_name.lower(), (region_name, []))[1].append((fname, text, key))
        for lname, (name, docs) in groups.items():
            region: Any = next((r for r in self.domain_regions() if r.name.lower() == lname), None) \
                or SimpleNamespace(name=name, kind="domain", biome=None)  # store only – no new area on the map
            try:
                store = await self.vectors.region_store(region, create=True)
                if store is None:
                    continue
                for i in range(0, len(docs), 20):
                    await self.vectors.add(store, [(fn, text) for fn, text, _ in docs[i:i + 20]])
            except Exception as exc:  # noqa: BLE001
                out["errors"].append(f"{name}: {exc}")
                continue
            self.archive_imported.update(k for _, _, k in docs)
            out["imported"] += len(docs)
            out["stores"].append(store.name)
        if out["imported"]:
            self.persist.kv()
            self.log(f"📚 Imported {out['imported']} archived mission report(s) into "
                     f"{len(out['stores'])} region knowledge base(s)", level="success")
        for err in out["errors"]:
            self.log(f"Mission archive import: {err}", level="warn")
        return out

    # ------------------------------------------------------------ regions
    def _create_region(self, name: str, description: str, biome: str, *, kind: str = "domain",
                       region_id: str | None = None) -> Region:
        idx = self._region_index
        self._region_index += 1
        if biome not in BIOMES or (kind == "domain" and biome == "capital"):
            biome = guess_biome(f"{name} {description}")
        region = Region(id=region_id or f"region-{uuid.uuid4().hex[:6]}", name=name, description=description,
                        biome=biome, index=idx, kind=kind,
                        center=hexgrid.territory_cell(hexgrid.region_center(idx)))
        self.regions[region.id] = region
        self.persist.region(region)
        self.persist.kv()
        self.bus.emit("region_created", region=region.to_dict())
        return region

    def create_region(self, name: str, description: str, biome: str) -> Region:
        region = self._create_region(short(name, 40), short(description, 200), biome)
        b = BIOMES[region.biome]
        self.log(f"HQ charted a new region: {region.name} ({b.icon} {b.label})", level="success")
        self._emit_stats()
        return region

    def update_region(self, region_id: str, *, name: str | None = None, biome: str | None = None) -> Region | None:
        region = self.regions.get(region_id)
        if region is None or region.kind != "domain":
            return None
        if name and name.strip():
            region.name = short(name.strip(), 40)
        if biome in DOMAIN_BIOMES:
            region.biome = biome
        self.persist.region(region)
        self.bus.emit("region_updated", region=region.to_dict())
        return region

    def domain_regions(self) -> list[Region]:
        return [r for r in self.regions.values() if r.kind == "domain"]

    def region_missions(self, region_id: str) -> list[Topic]:
        return [t for t in self.topics.values() if t.region_id == region_id and t.kind == "mission"]

    def place_topic(self, topic: Topic, region: Region) -> None:
        """Give a topic the first free territory slot of a region (full region → sibling region)."""
        used = {t.center for t in self.topics.values() if t.center is not None}
        for slot in hexgrid.region_slots(region.index):
            cell = hexgrid.territory_cell(slot)
            if cell not in used:
                topic.center, topic.region_id = cell, region.id
                self.topic_changed(topic)
                return
        self.place_topic(topic, self.create_region(f"{region.name} II", region.description, region.biome))

    def finish_topic(self, topic: Topic, status: str, result: str | None) -> None:
        topic.status, topic.result, topic.finished_at = status, result, time.time()
        if status == "completed":
            self.stats["missions_completed"] += 1
        elif status == "failed":
            self.stats["missions_failed"] += 1
        self.topic_changed(topic)
        self._emit_stats()
        self.log(f"Mission '{topic.title}' {status}", topic_id=topic.id,
                 level="success" if status == "completed" else "warn")

    def remove_topic(self, topic_id: str) -> bool:
        topic = self.topics.get(topic_id)
        if topic is None or topic.kind != "mission" or topic.status == "active":
            return False
        for aid in [a.id for a in self.agents.values() if a.topic_id == topic_id]:
            agent = self.agents.pop(aid)
            self.occupied.pop(agent.position, None)
        del self.topics[topic_id]
        self._drop_worktrees(self.intel.pop(topic_id, None))  # mission branches with commits are kept
        self._toolboxes.pop(topic_id, None)
        self.vectors.links.pop(topic_id, None)
        for t in self.tasks.list(context_id=topic_id):
            self.tasks.tasks.pop(t.id, None)
        self.persist.delete_topic(topic_id)
        self.bus.emit("topic_removed", topic_id=topic_id)
        return True

    async def resume_topic(self, topic_id: str) -> Topic | None:
        """Continue a mission that was interrupted by a server restart."""
        topic = self.topics.get(topic_id)
        if topic is None or topic.kind != "mission" or topic.status != "interrupted":
            return None
        if topic.root_agent_id:  # team exists → a feedback round re-tasks the same (veteran) team
            return await self.send_followup(topic_id, "The mission was interrupted by a server restart. "
                                            "Resume it and deliver the complete result.")
        assert self.commander is not None  # interrupted during the HQ survey → start over with the same intel
        topic.status, topic.finished_at = "active", None
        self.topic_changed(topic)
        msg = Message.of(topic.goal, role="user", context_id=topic.id,
                         metadata={"topic_id": topic.id, "mode": topic.mode, "title": topic.title})
        self.emit_message("user", self.commander.id, "request", topic.goal)
        self._dispatch(msg)
        return topic

    async def start_mission(self, goal: str, *, title: str | None = None, mode: str = "auto",
                            swarm_size: int | None = None, model: str | None = None,
                            region_id: str | None = None, context: list[ContextItem] | None = None,
                            profile: str | None = None, new_region: dict[str, str] | None = None,
                            proposal_id: str | None = None, team: str | None = None) -> Topic:
        """Create a mission and hand it to the Commander via A2A (it surveys + places the territory).

        Context travels A2A-natively: files as FileParts, references as a DataPart.
        ``new_region`` charts a region first (approved operations proposal); ``proposal_id`` marks that
        proposal as approved."""
        assert self.commander is not None
        if new_region and not region_id:
            existing = next((r for r in self.domain_regions()
                             if r.name.lower() == str(new_region.get("name", "")).strip().lower()), None)
            region_id = (existing or self.create_region(
                str(new_region.get("name") or title or "New Frontier"), str(new_region.get("description") or goal),
                str(new_region.get("biome") or ""))).id
        topic = self.create_topic(title or short(goal, 48), goal, mode)
        if team:
            topic.team = team
            self.topic_changed(topic)
        if proposal_id:
            self.ops.approved(proposal_id, topic.id)
        md = {"topic_id": topic.id, "mode": topic.mode, "title": topic.title, "swarm_size": swarm_size,
              "model": model, "region_id": region_id if region_id in self.regions else None,
              "profile": profile if self.profiles.get(profile) else None}
        msg = Message(role="user", parts=items_to_parts(goal, context or []), context_id=topic.id,
                      metadata={k: v for k, v in md.items() if v})
        self.emit_message("user", self.commander.id, "request", goal)
        self._dispatch(msg)
        return topic

    async def send_followup(self, topic_id: str, text: str,
                            context: list[ContextItem] | None = None) -> Topic | None:
        """Feedback for a finished mission → same A2A context → Commander re-tasks the same team."""
        assert self.commander is not None
        topic = self.topics.get(topic_id)
        if (topic is None or topic.kind != "mission" or topic.status == "active" or not topic.root_agent_id
                or topic.feedback_queued):
            return None
        topic.feedback_queued = True
        msg = Message(role="user", parts=items_to_parts(text, context or []), context_id=topic.id,
                      metadata={"topic_id": topic.id})
        self.emit_message("user", self.commander.id, "request", text)
        self._dispatch(msg)
        return topic

    def _dispatch(self, msg: Message) -> None:
        assert self.commander is not None

        async def _go() -> None:
            client = self.client_for(self.commander)
            with contextlib.suppress(Exception):
                async for _ in client.stream(msg):
                    pass

        t = asyncio.create_task(_go())
        self._background.add(t)
        t.add_done_callback(self._background.discard)

    async def cancel_topic(self, topic_id: str) -> bool:
        topic = self.topics.get(topic_id)
        if topic is None or topic.status != "active" or not topic.task_id:
            return False
        await self.tasks.cancel(topic.task_id)
        return True

    # ------------------------------------------------------------- agents
    def get_agent(self, agent_id: str) -> Agent | None:
        return self.agents.get(agent_id)

    def _next_name(self, cls: type[Agent], hint: str | None) -> str:
        sign = CALLSIGNS[self._callsign % len(CALLSIGNS)]
        num = self._callsign // len(CALLSIGNS)
        self._callsign += 1
        sign = sign if not num else f"{sign}-{num + 1}"
        if hint:
            return f"{hint} {sign}"
        return f"{cls.title} {sign}"

    def _place(self, topic: Topic, parent: Agent | None) -> tuple[int, int]:
        if topic.center is None:
            raise RuntimeError(f"Topic {topic.id} has not been placed on the map yet")
        anchor = parent.position if parent and parent.topic_id == topic.id else topic.center
        if anchor not in self.occupied and (parent is None or parent.topic_id != topic.id):
            return anchor
        for cap in (hexgrid.TERRITORY_RADIUS, hexgrid.TERRITORY_RADIUS + 1, 99):
            for radius in range(1, 14):
                for cell in hexgrid.ring(anchor, radius):
                    if cell not in self.occupied and hexgrid.distance(cell, topic.center) <= cap:
                        return cell
        return anchor

    def spawn(self, cls: type[Agent], *, topic_id: str, parent: Agent | None, name: str | None = None,
              name_hint: str | None = None, **kwargs: Any) -> Agent:
        active = sum(1 for a in self.agents.values() if a.status in ACTIVE_STATES)
        if active >= self.settings.max_agents:
            raise RuntimeError(f"Agent limit reached ({self.settings.max_agents})")
        topic = self.topics[topic_id]
        if name is None:
            spec = self.profiles.get(str(kwargs.get("specialty") or ""))
            label = spec.title if spec else cls.title
            name = self._next_name(cls, name_hint or label)
        agent = cls(self, name=name, topic_id=topic_id, parent_id=parent.id if parent else None, **kwargs)
        agent.position = self._place(topic, parent)
        self.occupied[agent.position] = agent.id
        self.agents[agent.id] = agent
        self.persist.agent(agent)
        if parent is not None and agent.id not in parent.children:
            parent.children.append(agent.id)
            self.agent_changed(parent)
        self.stats["agents_spawned"] += 1
        self.bus.emit("agent_spawned", agent=agent.to_dict())
        self._emit_stats()
        return agent

    # ------------------------------------------------------------ veterans
    def is_reusable(self, agent: Agent) -> bool:
        return (agent.id in self.agents and not agent.persistent and agent.status in REUSABLE_STATES
                and agent._inflight == 0)

    def find_veteran(self, cls: type[Agent], *, region_id: str | None, parent: Agent,
                     specialty: str | None = None, profile: str | None = None) -> Agent | None:
        """Best idle unit of exactly ``cls`` in the region: own former units first, then same
        territory, then anywhere in the region; most experienced first."""
        if region_id is None:
            return None
        best, best_rank = None, None
        for a in self.agents.values():
            if type(a) is not cls or not self.is_reusable(a):
                continue
            spec = getattr(a, "specialty", None)
            if specialty is not None and spec is not None and spec.key != specialty:
                continue
            if profile is not None and a.profile_key != profile:
                continue
            topic = self.topics.get(a.topic_id)
            if topic is None or topic.region_id != region_id:
                continue
            rank = (0 if a.parent_id == parent.id else 1 if a.topic_id == parent.topic_id else 2, -a.xp)
            if best_rank is None or rank < best_rank:
                best, best_rank = a, rank
        return best

    def recruit(self, agent: Agent, *, parent: Agent, topic_id: str, model: str | None) -> Agent:
        """Re-deploy an idle veteran under ``parent`` – marching to another territory if needed.

        Synchronously marks the unit busy, so parallel planners can't recruit it twice."""
        topic = self.topics[topic_id]
        if agent.parent_id != parent.id:
            old_parent = self.agents.get(agent.parent_id or "")
            if old_parent is not None and agent.id in old_parent.children:
                old_parent.children.remove(agent.id)
                self.agent_changed(old_parent)
            agent.parent_id = parent.id
            parent.children.append(agent.id)
            self.agent_changed(parent)
        agent.depth = parent.depth + 1
        agent.model = model
        moved = agent.topic_id != topic_id
        if moved:
            self.occupied.pop(agent.position, None)
            agent.topic_id = topic_id
            agent.position = self._place(topic, parent)
            self.occupied[agent.position] = agent.id
        agent.recruitments += 1
        agent.gain_xp(25)
        agent.set_status("working", "Marching to new orders" if moved else "Reporting for duty")
        self.stats["veterans_redeployed"] += 1
        self._emit_stats()
        self.log(f"Veteran {agent.name} (Lv {agent.level}) {'re-deployed to' if moved else 'rejoins'} "
                 f"'{topic.title}'", agent=agent, level="success")
        return agent

    async def register_remote(self, url: str, api_key: str | None = None,
                              headers: dict[str, str] | None = None) -> RemoteAgent:
        assert self.http is not None
        hdrs = dict(headers or {})
        if api_key:
            hdrs["Authorization"] = f"Bearer {api_key}"
        client = A2AClient(url, self.http, headers=hdrs)
        card = await client.get_card()
        if ALLIES_TOPIC_ID not in self.topics:
            t = self._new_topic("Allied Agents", "External A2A agents", kind="allies", topic_id=ALLIES_TOPIC_ID)
            t.status = "base"
            self.place_topic(t, self.regions[HQ_REGION_ID])
        agent = self.spawn(RemoteAgent, topic_id=ALLIES_TOPIC_ID, parent=None, name=card.name,
                           remote_url=card.url or url, remote_card=card, headers=hdrs)
        self.log(f"Ally joined: {card.name} ({url})", agent=agent, level="success")
        return agent

    def client_for(self, agent: Agent) -> A2AClient:
        assert self.http is not None
        return A2AClient(agent.url, self.http)

    # ---------------------------------------------------------- telemetry
    def emit(self, type_: str, **data: Any) -> None:
        self.bus.emit(type_, **data)

    def agent_changed(self, agent: Agent) -> None:
        if agent.id not in self.agents:
            return
        self.persist.agent(agent)
        self.bus.emit("agent_updated", agent=agent.to_dict())
        active = sum(1 for a in self.agents.values() if a.status in ACTIVE_STATES)
        self.stats["peak_active"] = max(self.stats["peak_active"], active)
        if active != self._last_active:  # also push decreases, so the HUD drops back to 0
            self._last_active = active
            self._emit_stats()

    def add_usage(self, tokens: int, cost: float, topic_id: str | None = None) -> None:
        self.stats["tokens"] += tokens
        self.stats["cost"] += cost
        topic = self.topics.get(topic_id or "")
        if topic is not None and topic.kind == "mission":
            topic.tokens += tokens
            topic.cost += cost
            self.persist.topic(topic)
        self._emit_stats()

    def emit_message(self, from_id: str, to_id: str, kind: str, text: str) -> None:
        self.stats["a2a_messages"] += 1
        self.bus.emit("a2a_message", **{"from": from_id, "to": to_id, "kind": kind, "text": short(text, 200)})

    def log(self, text: str, *, agent: Agent | None = None, topic_id: str | None = None,
            level: str = "info") -> None:
        log.log({"error": logging.ERROR, "warn": logging.WARNING}.get(level, logging.INFO), text)
        self.bus.emit("log", text=text, level=level, agent_id=agent.id if agent else None,
                      topic_id=topic_id or (agent.topic_id if agent else None))

    def _emit_stats(self) -> None:
        self.persist.kv()
        self.bus.emit("stats", stats=self.stats_dict())

    def stats_dict(self) -> dict[str, Any]:
        active = sum(1 for a in self.agents.values() if a.status in ACTIVE_STATES)
        return {**self.stats, "cost": round(self.stats["cost"], 6), "active_agents": active,
                "total_agents": len(self.agents), "regions": len(self.domain_regions()),
                "active_missions": sum(1 for t in self.topics.values() if t.status == "active")}

    def snapshot(self) -> dict[str, Any]:
        return {
            "regions": [r.to_dict() for r in self.regions.values()],
            "topics": [t.to_dict() for t in self.topics.values()],
            "agents": [a.to_dict() for a in self.agents.values()],
            "stats": self.stats_dict(),
            "config": {
                "biomes": [{"key": k, "label": BIOMES[k].label, "icon": BIOMES[k].icon, "hint": BIOMES[k].hint}
                           for k in DOMAIN_BIOMES],
                "mock": self.settings.mock,
                "default_model": self.settings.default_model,
                "model_problems": self.model_problems,
                "proxy": bool(self.settings.proxy_url),
                "max_swarm_size": self.settings.max_swarm_size,
                "default_swarm_size": self.settings.default_swarm_size,
                "commander_url": self.commander.url if self.commander else None,
                "context_roots": self.settings.context_roots,
                "mcp": self.mcp.available,
                "vectors": self.vectors.kind,
                "persistence": self.persist.info(),
            },
            "ops": self.ops.snapshot(),
        }

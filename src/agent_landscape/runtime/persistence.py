"""Runtime ⇄ SQLite: write-behind persistence and restore at startup.

The runtime stays in-memory (fast, simple). Every change marks the object *dirty*; a flusher writes the
dirty set roughly once per second in one transaction. Flushing happens on the event-loop thread, so it
always sees a consistent state and batches never overtake each other.

On startup everything is loaded back. Work that was running when the server stopped cannot continue
mid-way, so missions become ``interrupted`` (resumable), agents ``canceled`` and open A2A tasks canceled.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, fields
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..a2a.types import TERMINAL_STATES, Message, Task, TaskState, TaskStatus
from ..storage.db import Database

if TYPE_CHECKING:
    from .runtime import Runtime

log = logging.getLogger(__name__)
INTERRUPTED = "interrupted"


def _dumps(obj: Any) -> str:
    return json.dumps(obj, default=str, ensure_ascii=False)


class NullPersistence:
    """No-op stand-in (persistence disabled, e.g. tests)."""

    enabled = False

    def agent(self, agent: Any) -> None: ...
    def topic(self, topic: Any) -> None: ...
    def region(self, region: Any) -> None: ...
    def task_id(self, task_id: str) -> None: ...
    def intel(self, topic_id: str) -> None: ...
    def links(self, topic_id: str) -> None: ...
    def kv(self) -> None: ...
    def delete_topic(self, topic_id: str) -> None: ...
    def restore(self) -> bool: return False
    def start(self) -> None: ...
    def flush(self) -> None: ...
    def freeze(self) -> None: ...
    async def stop(self) -> None: ...
    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]] | None: return None
    def info(self) -> dict[str, Any]: return {"enabled": False}


class Persistence(NullPersistence):
    enabled = True

    def __init__(self, runtime: "Runtime", db: Database, interval: float = 1.0) -> None:
        self.rt = runtime
        self.db = db
        self.interval = interval
        self.frozen = False
        self._task: asyncio.Task | None = None
        self._agents: set[str] = set()
        self._topics: set[str] = set()
        self._regions: set[str] = set()
        self._tasks: set[str] = set()
        self._intel: set[str] = set()
        self._links: set[str] = set()
        self._deleted: set[str] = set()
        self._kv = False
        self.stats: dict[str, Any] = {"flushes": 0, "rows": 0, "errors": 0, "last_flush": None, "restored": {}}

    # ------------------------------------------------------------ marking
    def agent(self, agent: Any) -> None:
        if not self.frozen:
            self._agents.add(agent.id)

    def topic(self, topic: Any) -> None:
        if not self.frozen:
            self._topics.add(topic.id)

    def region(self, region: Any) -> None:
        if not self.frozen:
            self._regions.add(region.id)

    def task_id(self, task_id: str) -> None:
        if not self.frozen:
            self._tasks.add(task_id)

    def intel(self, topic_id: str) -> None:
        if not self.frozen:
            self._intel.add(topic_id)

    def links(self, topic_id: str) -> None:
        if not self.frozen:
            self._links.add(topic_id)

    def kv(self) -> None:
        if not self.frozen:
            self._kv = True

    def delete_topic(self, topic_id: str) -> None:
        if not self.frozen:
            self._deleted.add(topic_id)
            for s in (self._topics, self._intel, self._links):
                s.discard(topic_id)

    # ------------------------------------------------------------ records
    @staticmethod
    def agent_record(a: Any) -> dict[str, Any]:
        extra: dict[str, Any] = {}
        if getattr(a, "specialty", None) is not None:
            extra["specialty"] = a.specialty.key
        if getattr(a, "persona", None) is not None:
            extra["persona"] = list(a.persona)
        if getattr(a, "remote_url", None):
            extra.update(remote_url=a.remote_url, remote_card=a.remote_card.dump(), headers=a.headers)
        return {
            "id": a.id, "cls": type(a).__name__, "role": a.role, "name": a.name, "topic_id": a.topic_id,
            "parent_id": a.parent_id, "model": a.model, "depth": a.depth, "mode": a.mode, "options": a.options,
            "children": list(a.children), "status": a.status, "activity": a.activity, "tokens": a.tokens,
            "cost": a.cost, "xp": a.xp, "tasks_done": a.tasks_done, "last_input": a.last_input,
            "last_output": a.last_output, "objective": a.objective, "memory": a.memory,
            "recruitments": a.recruitments, "position": list(a.position), "created_at": a.created_at,
            "profile": a.profile_key, "pinned_model": a.pinned_model,
            "updated_at": a.updated_at, "extra": extra,
        }

    def collect(self) -> dict[str, Any] | None:
        """Snapshot all dirty objects into rows (event-loop thread → consistent)."""
        from .reports import _input, _output

        rt, now = self.rt, time.time()
        b: dict[str, Any] = {"regions": [], "topics": [], "agents": [], "tasks": [], "intel": [], "kv": [],
                             "docs": {}, "links": {}, "fts": [], "deletes": sorted(self._deleted)}
        for rid in self._regions:
            r = rt.regions.get(rid)
            if r is not None:
                b["regions"].append((r.id, _dumps(asdict(r)), r.created_at))
        for tid in self._topics:
            t = rt.topics.get(tid)
            if t is None:
                continue
            b["topics"].append((t.id, t.region_id, t.kind, t.status, t.title, _dumps(asdict(t)), t.created_at, now))
            if t.kind == "mission" and t.result:
                body = f"{t.goal}\n\n{t.result}" + ("\n\nFeedback: " + " | ".join(t.feedback) if t.feedback else "")
                b["fts"].append(("mission", t.id, t.id, t.title, body))
        for aid in self._agents:
            a = rt.agents.get(aid)
            if a is not None:
                b["agents"].append((a.id, a.topic_id, a.role, a.name, _dumps(self.agent_record(a)), now))
        for task_id in self._tasks:
            task = rt.tasks.get(task_id)
            if task is None:
                continue
            md = task.metadata or {}
            b["tasks"].append((task.id, task.context_id, md.get("agent_id"), task.status.state.value,
                               _dumps(task.dump()), md.get("created_at") or now, now))
            if task.status.state == TaskState.completed and md.get("agent_id") != "commander":
                out = _output(task)
                if out:
                    title = f"{md.get('agent_name') or md.get('agent_id')} – {md.get('subtask') or md.get('title') or ''}"
                    b["fts"].append(("artifact", task.id, task.context_id, title, f"{_input(task)}\n\n{out}"))
        for tid in self._intel:
            intel = rt.intel.get(tid)
            if intel is None:
                continue
            b["intel"].append((tid, _dumps({"roots": [str(p) for p in intel.roots],
                                            "tools": [asdict(t) for t in intel.tools],
                                            "stores": [asdict(s) for s in intel.stores],
                                            "repos": [r.to_dict() for r in intel.repos],
                                            "summary": intel.summary})))
            b["docs"][tid] = [(d.name, d.source, d.origin, d.text) for d in intel.docs.values()]
        for tid in self._links:
            b["links"][tid] = [(sid, _dumps(asdict(s))) for sid, s in rt.vectors.links.get(tid, {}).items()]
        if self._kv:
            b["kv"] = [
                ("stats", _dumps(rt.stats)),
                ("counters", _dumps({"territory_index": rt._territory_index, "region_index": rt._region_index,
                                     "callsign": rt._callsign})),
                ("kb_usage", _dumps({k: sorted(v) for k, v in rt.vectors.usage.items()})),
                ("ops", _dumps(rt.ops.dump())),
                ("teams", _dumps(rt.teams.dump())),
                ("archive_imported", _dumps(sorted(rt.archive_imported))),
            ]
        for s in (self._agents, self._topics, self._regions, self._tasks, self._intel, self._links, self._deleted):
            s.clear()
        self._kv = False
        if not any(b[k] for k in b):
            return None
        return b

    # -------------------------------------------------------------- flush
    def flush(self) -> None:
        if self.frozen:
            return
        try:
            batch = self.collect()
            if batch:
                self.stats["rows"] += self.db.write(batch)
                self.stats["flushes"] += 1
                self.stats["last_flush"] = time.time()
        except Exception:  # noqa: BLE001 – never take the runtime down
            self.stats["errors"] += 1
            log.exception("Persistence flush failed")

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            self.flush()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="persistence-flush")

    def freeze(self) -> None:
        """Final flush; afterwards changes are ignored (shutdown cancellations must not be persisted)."""
        if not self.frozen:
            self.flush()
            self.frozen = True

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self.freeze()
        self.db.close()

    # ------------------------------------------------------------ restore
    def restore(self) -> bool:
        from ..agents.base import ACTIVE_STATES
        from ..context.intel import Doc, MissionIntel
        from ..context.mcp import MCPTool
        from ..context.repos import MissionRepo
        from ..context.vectorstore import StoreInfo
        from .regions import Region
        from .runtime import Topic

        rt = self.rt
        data = self.db.load(task_limit=rt.tasks.max_tasks)
        if not data["regions"] and not data["topics"]:
            return False
        kv = data["kv"]
        rt.stats.update(kv.get("stats") or {})
        counters = kv.get("counters") or {}
        rt._territory_index = int(counters.get("territory_index", 0))
        rt._region_index = int(counters.get("region_index", 0))
        rt._callsign = int(counters.get("callsign", 0))

        rnames = {f.name for f in fields(Region)}
        for d in data["regions"]:
            d = {k: v for k, v in d.items() if k in rnames}
            d["center"] = tuple(d["center"])
            rt.regions[d["id"]] = Region(**d)
        tnames = {f.name for f in fields(Topic)}
        for d in data["topics"]:
            d = {k: v for k, v in d.items() if k in tnames}
            d["center"] = tuple(d["center"]) if d.get("center") else None
            rt.topics[d["id"]] = Topic(**d)
        skipped = 0
        for d in data["agents"]:
            try:
                a = self._build_agent(d)
            except Exception:  # noqa: BLE001 – one broken record must not block startup
                skipped += 1
                log.exception("Could not restore agent %s", d.get("id"))
                continue
            rt.agents[a.id] = a
            rt.occupied[a.position] = a.id
        for d in data["tasks"]:
            task = Task.model_validate(d)
            rt.tasks.tasks[task.id] = task
        for tid, d in data["intel"].items():
            docs = {r["name"]: Doc(name=r["name"], text=r["text"] or "", source=r["source"] or "file",
                                   origin=r["origin"] or "") for r in data["docs"].get(tid, [])}
            rt.intel[tid] = MissionIntel(
                docs=docs, roots=[Path(p) for p in d.get("roots", []) if Path(p).exists()],
                tools=[MCPTool(**t) for t in d.get("tools", [])],
                stores=[StoreInfo(**s) for s in d.get("stores", [])], summary=d.get("summary", []),
                repos=[MissionRepo.from_dict(r) for r in d.get("repos", []) if Path(r.get("path", "")).exists()])
        for tid, rows in data["links"].items():
            rt.vectors.links[tid] = {sid: StoreInfo(**s) for sid, s in rows}
        rt.vectors.usage = {k: set(v) for k, v in (kv.get("kb_usage") or {}).items()}
        rt.ops.restore(kv.get("ops"))
        rt.teams.restore(kv.get("teams"))
        rt.archive_imported = set(kv.get("archive_imported") or [])

        interrupted = self._interrupt(ACTIVE_STATES)
        self.stats["restored"] = {"regions": len(rt.regions), "missions": sum(t.kind == "mission" for t in rt.topics.values()),
                                  "agents": len(rt.agents), "tasks": len(rt.tasks.tasks), "interrupted": interrupted,
                                  "skipped_agents": skipped}
        log.info("Restored from %s: %s", self.db.path, self.stats["restored"])
        return True

    def _build_agent(self, d: dict[str, Any]) -> Any:
        from ..a2a.types import AgentCard
        from ..agents.ops import ChannelMonitor, CommsLead
        from ..agents.units import Commander, Orchestrator, RemoteAgent, SwarmLeader, SwarmMember, Worker

        classes = {c.__name__: c for c in (Commander, Orchestrator, Worker, SwarmLeader, SwarmMember, RemoteAgent,
                                           ChannelMonitor, CommsLead)}
        cls = classes[d["cls"]]
        extra = d.get("extra") or {}
        kw: dict[str, Any] = dict(name=d["name"], topic_id=d["topic_id"], parent_id=d.get("parent_id"),
                                  model=d.get("model"), depth=d.get("depth", 0), mode=d.get("mode", "auto"),
                                  agent_id=d["id"], **(d.get("options") or {}))
        if cls is Worker:
            kw["specialty"] = extra.get("specialty", "researcher")
        elif cls is SwarmMember and extra.get("persona"):
            kw["persona"] = tuple(extra["persona"])
        elif cls is RemoteAgent:
            kw.update(remote_url=extra["remote_url"], remote_card=AgentCard.model_validate(extra["remote_card"]),
                      headers=extra.get("headers") or {})
        a = cls(self.rt, **kw)
        if d.get("profile") and self.rt.profiles.get(d["profile"]) is not None:
            a.profile_key = d["profile"]
        for key in ("children", "status", "activity", "tokens", "cost", "xp", "tasks_done", "last_input",
                    "last_output", "objective", "memory", "recruitments", "created_at", "updated_at",
                    "pinned_model"):
            if key in d:
                setattr(a, key, d[key])
        a.position = tuple(d.get("position") or (0, 0))
        return a

    def _interrupt(self, active_states: set[str]) -> int:
        """Work that was running when the server stopped cannot resume mid-way."""
        rt, now, n = self.rt, time.time(), 0
        for t in rt.topics.values():
            if t.kind == "mission" and t.status == "active":
                t.status, t.finished_at = INTERRUPTED, now
                n += 1
                self.topic(t)
            if t.feedback_queued:
                t.feedback_queued = False
                self.topic(t)
        for a in rt.agents.values():
            if a.persistent and a.status != "idle":
                a.status, a.activity = "idle", "Awaiting orders"
                self.agent(a)
            elif not a.persistent and a.status in active_states:
                a.status, a.activity = "canceled", "Interrupted by a server restart"
                self.agent(a)
        for task in rt.tasks.tasks.values():
            if task.status.state not in TERMINAL_STATES:
                task.status = TaskStatus(state=TaskState.canceled, message=Message.of(
                    "Interrupted by a server restart", role="agent", task_id=task.id, context_id=task.context_id))
                self.task_id(task.id)
        return n

    # ------------------------------------------------------------- access
    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]] | None:
        return self.db.search(query, limit) if self.db.fts else None

    def info(self) -> dict[str, Any]:
        return {"enabled": True, "path": str(self.db.path), "schema": self.db.version, "fts": self.db.fts,
                "flushes": self.stats["flushes"], "errors": self.stats["errors"], "restored": self.stats["restored"]}

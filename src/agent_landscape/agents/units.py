"""Concrete unit types: Commander, Orchestrator, Worker, SwarmLeader, SwarmMember, RemoteAgent."""

from __future__ import annotations

import asyncio
import json
import random
import re
from typing import Any

from ..a2a.client import A2AClient
from ..a2a.task_manager import TaskContext
from ..a2a.types import AgentCard, AgentSkill, Message, result_text
from ..context.intel import items_from_message
from ..context.vectorstore import recall_briefing
from ..ops.proposals import mock_proposal, parse_proposal, proposal_system
from ..ops.sources import InboxMessage
from ..runtime.regions import Region, mock_survey, parse_survey, survey_system
from . import mock
from .base import Agent, short
from .roles import plan_system_prompt, synth_system_prompt

SYNTH_DEFAULT = ("Your team delivered the reports below. Merge them into one coherent, well-structured final "
                 "deliverable for the mission. Resolve conflicts, remove redundancy, keep the important details.")
SWARM_SYNTH_DEFAULT = ("Several agents answered the same task from different perspectives. Build the consensus "
                       "answer: keep the strongest ideas, note important disagreements, and give a clear recommendation.")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


async def gather_with_progress(agent: Agent, label: str, coros: list) -> list[Any]:
    """Run coroutines concurrently, updating the agent's activity as they finish."""
    total, done = len(coros), 0

    async def track(c):
        nonlocal done
        try:
            return await c
        finally:
            done += 1
            agent.set_status("waiting", f"{label} ({done}/{total})")

    agent.set_status("waiting", f"{label} (0/{total})")
    return await asyncio.gather(*(track(c) for c in coros), return_exceptions=True)


def _reports(names: list[str], results: list[Any], agent: Agent | None = None) -> tuple[str, int]:
    parts, ok = [], 0
    for name, res in zip(names, results):
        if isinstance(res, BaseException):
            if agent is not None:
                agent.runtime.log(f"{name}: delegation failed – {type(res).__name__}: {res}", agent=agent,
                                  level="error")
            parts.append(f"## {name}\nFAILED: {res}")
        else:
            ok += 1
            parts.append(f"## {name}\n{res}")
    return "\n\n".join(parts), ok


# --------------------------------------------------------------------- Worker
class Worker(Agent):
    role = "worker"
    title = "Specialist"
    description = "A specialist agent that executes a focused subtask."

    def __init__(self, runtime: Any, *, specialty: str = "researcher", **kw: Any) -> None:
        p = runtime.profiles.resolve(specialty, "worker", "researcher")
        self.profile_key, self._snapshot = p.key, p
        self.icon, self.title, self.description = p.icon, p.title, p.description or p.prompt[:200]
        super().__init__(runtime, **kw)

    @property
    def specialty(self) -> Any:
        """The worker's profile (live; falls back to the version it was spawned with if it was deleted)."""
        return self.profile or self._snapshot

    def skills(self) -> list[AgentSkill]:
        p = self.specialty
        return [AgentSkill(id=p.key, name=p.title, description=p.description or p.prompt[:200], tags=[p.key, "worker"])]

    async def run(self, text: str, ctx: TaskContext) -> str:
        p = self.specialty
        self.icon, self.title = p.icon, p.title
        return await self.think(p.prompt, text, activity=f"{p.title}: {self.objective}",
                                mock=mock.mock_report(self.name, text))

    def to_dict(self) -> dict[str, Any]:
        return {**super().to_dict(), "specialty": self.specialty.key}


# --------------------------------------------------------------- Swarm units
class SwarmMember(Agent):
    role = "swarm_member"
    icon = "🐝"
    title = "Drone"
    description = "A swarm member contributing one perspective."

    def __init__(self, runtime: Any, *, persona: str | tuple | list = "swarm-optimist", **kw: Any) -> None:
        if isinstance(persona, (tuple, list)):  # legacy records: (title, description)
            persona = f"swarm-{_slug(str(persona[0]))}"
        p = runtime.profiles.resolve(persona, "swarm_member", "swarm-optimist")
        self.profile_key, self._snapshot = p.key, p
        super().__init__(runtime, **kw)
        self.title = f"Drone · {p.title}"

    @property
    def persona(self) -> tuple[str, str]:
        p = self.profile or self._snapshot
        return p.title, p.description

    async def run(self, text: str, ctx: TaskContext) -> str:
        p = self.profile or self._snapshot
        return await self.think(p.prompt, text, activity=f"{p.title} view: {self.objective}",
                                mock=mock.mock_report(f"{self.name} ({p.title})", text))

    def to_dict(self) -> dict[str, Any]:
        return {**super().to_dict(), "persona": self.persona[0]}


class SwarmLeader(Agent):
    role = "swarm_leader"
    icon = "🌀"
    title = "Hive Leader"
    description = "Releases a swarm of agents with diverse perspectives and merges their answers."

    def __init__(self, runtime: Any, *, profile: str | None = None, **kw: Any) -> None:
        self.profile_key = runtime.profiles.resolve(profile, "swarm_leader", "hive-leader").key
        super().__init__(runtime, **kw)

    async def run(self, text: str, ctx: TaskContext) -> str:
        rt, s = self.runtime, self.runtime.settings
        personas = rt.profiles.by_kind("swarm_member")
        if not personas:
            raise RuntimeError("No enabled swarm member profiles")
        # reuse this hive's own idle drones first (feedback rounds / re-deployments)
        own = [rt.agents[c] for c in self.children
               if c in rt.agents and isinstance(rt.agents[c], SwarmMember) and rt.is_reusable(rt.agents[c])]
        size = int(ctx.metadata.get("swarm_size") or len(own) or self.options.get("swarm_size")
                   or s.default_swarm_size)
        size = max(1, min(size, s.max_swarm_size, max(len(personas), len(own))))
        members: list[SwarmMember] = [rt.recruit(m, parent=self, topic_id=self.topic_id, model=self.model)
                                      for m in own[:size]]  # type: ignore[misc]
        veterans = len(members)
        used = {m.profile_key for m in members}
        pool = [p for p in personas if p.key not in used]
        fresh = random.sample(pool, k=min(len(pool), size - veterans))
        members += [self.spawn(SwarmMember, persona=p.key, depth=self.depth + 1, model=self.model)  # type: ignore[misc]
                    for p in fresh]
        size = len(members)
        self.set_status("delegating", f"Releasing swarm of {size}" + (f" ({veterans} veterans)" if veterans else ""))
        await asyncio.sleep(0.4)  # let the UI play the spawn animation
        md = {"subtask": self.objective, "followup": ctx.metadata.get("followup")}
        results = await gather_with_progress(
            self, "Swarm working", [self.delegate(m, text, metadata=md) for m in members])
        report, ok = _reports([f"{m.name} ({m.persona[0]})" for m in members], results, self)
        if ok == 0:
            raise RuntimeError("The whole swarm failed")
        prof = self.profile or rt.profiles.resolve(None, "swarm_leader", "hive-leader")
        return await self.think(synth_system_prompt(prof, SWARM_SYNTH_DEFAULT), f"Task:\n{text}\n\nSwarm answers:\n{report}",
                                activity="Forming consensus", temperature=0.3, use_tools=False,
                                mock=mock.mock_synthesis(self.name, ok))


# --------------------------------------------------------------- Orchestrator
_JSON_RE = re.compile(r"\{.*\}", re.S)


def parse_plan(raw: str, limit: int) -> list[dict[str, Any]]:
    m = _JSON_RE.search(raw or "")
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    items = data.get("subtasks") if isinstance(data, dict) else None
    out = []
    for it in (items or [])[:limit]:
        if isinstance(it, dict) and (it.get("instructions") or it.get("title")):
            out.append(it)
    return out


class Orchestrator(Agent):
    role = "orchestrator"
    icon = "🏰"
    title = "Orchestrator"
    description = "Plans a mission, spawns sub-agents / swarms / sub-orchestrators and merges results."

    def __init__(self, runtime: Any, *, profile: str | None = None, **kw: Any) -> None:
        p = runtime.profiles.resolve(profile, "orchestrator", "orchestrator")
        self.profile_key = p.key
        self.icon = p.icon
        super().__init__(runtime, **kw)

    def _prof(self) -> Any:
        return self.profile or self.runtime.profiles.resolve(None, "orchestrator", "orchestrator")

    def _workers(self) -> list[Any]:
        """Specialists this orchestrator may plan with: the team's members, else every enabled worker."""
        workers = self.runtime.profiles.workers()
        topic = self.runtime.topics.get(self.topic_id)
        team = self.runtime.teams.get(topic.team) if topic is not None and topic.team else None
        if team is not None and team.members:
            members = [w for w in workers if w.key in team.members]
            return members or workers
        return workers

    def _model(self) -> str | None:
        """mission model > profile model > ORCHESTRATOR_MODEL > DEFAULT_MODEL"""
        return self.model or self._prof().model or self.runtime.settings.orchestrator_model

    def _pool(self) -> list[str]:
        """Auto model mode: the models this mission's orchestrators may assign per subtask."""
        topic = self.runtime.topics.get(self.topic_id)
        return list(topic.model_pool) if topic is not None else []

    def _pick_model(self, st: dict[str, Any]) -> str | None:
        """The subtask's model: the planner's choice if it is in the pool, else this orchestrator's own setting."""
        pool = self._pool()
        wanted = str(st.get("model") or "").strip()
        if not pool or not wanted:
            return self.model
        if wanted in pool:
            return wanted
        self.runtime.log(f"{self.name}: model '{wanted}' is not in the mission's pool – using the default",
                         agent=self, level="warn")
        return self.model

    def _tool_capable(self, model: str | None, prof: Any, st: dict[str, Any]) -> str | None:
        """A repo-editing specialist on a tool-less claude-code/* model can't change anything (it would only
        *describe* edits). Swap to a gateway model from the pool; if there is none, warn loudly."""
        rt = self.runtime
        repos = getattr(rt.intel.get(self.topic_id), "repos", [])
        if "repo_write" not in prof.tools or not any(r.write for r in repos):
            return model
        effective = model or prof.model or rt.settings.default_model
        if not str(effective).startswith("claude-code/"):
            return model
        title = str(st.get("title") or "Subtask")[:40]
        gateway = [m for m in self._pool() if not m.startswith("claude-code/")]
        if gateway:
            rt.log(f"{self.name}: '{title}' edits a repo – {effective} has no tools, using {gateway[0]}",
                   agent=self, level="warn")
            return gateway[0]
        rt.log(f"{self.name}: '{title}' edits a repo but runs on {effective} (no tools) – it can't change files. "
               "Pick a gateway model (or add one to the Auto pool).", agent=self, level="error")
        return model

    def _child_for(self, st: dict[str, Any]) -> Agent:
        s = self.runtime.settings
        model = self._pick_model(st)
        strategy = str(st.get("strategy", "single")).lower()
        if self.mode == "hierarchy" and strategy == "swarm":
            strategy = "single"
        if strategy == "delegate" and self.depth + 1 >= s.max_depth:
            strategy = "single"
        if strategy == "ally":
            ally = self.runtime.agents.get(str(st.get("ally_id", "")))
            if isinstance(ally, RemoteAgent):
                return ally
            strategy = "single"
        specialty: str | None = None
        kwargs: dict[str, Any]
        if strategy == "swarm":
            cls, kwargs = SwarmLeader, {"name_hint": "Hive"}
        elif strategy == "delegate":  # captains inherit their orchestrator's profile
            cls, kwargs = Orchestrator, {"name_hint": "Captain", "profile": self.profile_key}
        else:
            wanted = str(st.get("specialty", "")).lower()
            allowed = [w.key for w in self._workers()]
            if allowed and wanted not in allowed:  # team missions only use their members
                wanted = allowed[0]
            prof = self.runtime.profiles.resolve(wanted, "worker", allowed[0] if allowed else "researcher")
            specialty = prof.key
            cls, kwargs = Worker, {"specialty": specialty}
            model = self._tool_capable(model, prof, st)
        # prefer an idle veteran (own team first, then this region) over a fresh recruit
        rt = self.runtime
        veteran = rt.find_veteran(cls, region_id=rt.topics[self.topic_id].region_id, parent=self,
                                  specialty=specialty)
        if veteran is not None:
            return rt.recruit(veteran, parent=self, topic_id=self.topic_id, model=model)
        return self.spawn(cls, depth=self.depth + 1, model=model, **kwargs)

    @staticmethod
    def _team_entry(agent: Agent) -> tuple[str, str, str]:
        title = agent.objective or agent.title
        if isinstance(agent, Worker):
            return title, agent.specialty.key, "single"
        return title, "researcher", "swarm" if isinstance(agent, SwarmLeader) else "delegate"

    async def run(self, text: str, ctx: TaskContext) -> str:
        rt, s = self.runtime, self.runtime.settings
        followup = bool(ctx.metadata.get("followup"))
        swarm_size = ctx.metadata.get("swarm_size")
        allow_delegate = self.depth + 1 < s.max_depth
        allow_swarm = self.mode != "hierarchy"
        allies = [a.brief() for a in rt.agents.values() if isinstance(a, RemoteAgent)]
        team = [rt.agents[c] for c in self.children if c in rt.agents and rt.is_reusable(rt.agents[c])]
        prompt = f"Mission:\n{text}"
        if team:
            prompt += ("\n\nYour existing team (veterans with memory of their earlier work – reuse them where it "
                       "fits by giving them matching specialty/strategy):\n"
                       + "\n".join(f"- {a.name}: {spec} / {strat} – earlier: {short(t, 80)}"
                                   for a, (t, spec, strat) in ((a, self._team_entry(a)) for a in team)))
        raw = await self.think(
            plan_system_prompt(self._prof(), self._workers(), s.max_subtasks, allow_delegate, allow_swarm, allies,
                               models=self._pool()),
            prompt,
            activity="Revising battle plan" if followup else "Drafting battle plan",
            temperature=0.2,
            model=self._model(),
            mock=mock.mock_plan_from_team([self._team_entry(a) for a in team][:s.max_subtasks])
            if followup and team else mock.mock_plan(s.max_subtasks, allow_swarm=allow_swarm,
                                                     allow_delegate=allow_delegate and self.depth == 1,
                                                     code=any(r.write for r in getattr(
                                                         rt.intel.get(self.topic_id), "repos", [])),
                                                     models=self._pool()),
        )
        plan = parse_plan(raw, s.max_subtasks) or [
            {"title": "Execute mission", "instructions": text, "specialty": "researcher", "strategy": "single"}]
        self.runtime.log(f"{self.name} drafted a plan with {len(plan)} subtasks", agent=self)

        self.set_status("delegating", f"Dispatching {len(plan)} units")
        children = [self._child_for(st) for st in plan]
        if self._pool():
            self.runtime.log(f"{self.name} assigned models: " + ", ".join(
                f"{str(st.get('title') or 'Subtask')[:40]} → {c.model or 'default'}" for c, st in zip(children, plan, strict=True)),
                agent=self)
        await asyncio.sleep(0.4)
        coros = []
        for child, st in zip(children, plan):
            title = str(st.get("title") or "Subtask")[:80]
            prompt = (f"Overall mission:\n{short(text, 1500)}\n\nYour subtask: {title}\n"
                      f"{st.get('instructions', '')}")
            coros.append(self.delegate(child, prompt, metadata={
                "subtask": title, "followup": followup, "swarm_size": swarm_size}))
        results = await gather_with_progress(self, "Awaiting reports", coros)
        report, ok = _reports([f"{c.name} – {st.get('title', '')}" for c, st in zip(children, plan)], results, self)
        if ok == 0:
            raise RuntimeError("All sub-agents failed")
        return await self.think(synth_system_prompt(self._prof(), SYNTH_DEFAULT),
                                f"Mission:\n{text}\n\nTeam reports:\n{report}",
                                activity="Synthesizing reports", temperature=0.3, use_tools=False,
                                model=self._model(),
                                mock=mock.mock_synthesis(self.name, ok))


# ------------------------------------------------------------------ Commander
class Commander(Agent):
    """Persistent entry point. Each incoming A2A message becomes a mission (topic)."""

    role = "commander"
    icon = "👑"
    title = "Commander"
    description = ("Entry point of the agent landscape. Send a mission; it opens a new territory, deploys an "
                   "orchestrator (or swarm) with sub-agents and returns the final deliverable.")
    persistent = True

    def __init__(self, *args: Any, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.active_missions = 0
        # one survey at a time: concurrent missions must see regions charted a moment earlier
        self._survey_lock = asyncio.Lock()

    def skills(self) -> list[AgentSkill]:
        return [AgentSkill(
            id="mission", name="Run mission",
            description="Plans and executes a mission with a team of sub-agents and swarms.",
            tags=["orchestration", "multi-agent", "swarm"],
            input_modes=["text/plain", "text/markdown", "application/json"],
            examples=["Design a launch plan for our new mobile app",
                      "Research and compare vector databases for our RAG stack"])]

    def card(self) -> AgentCard:
        card = super().card()
        # context can be attached as FilePart (text files) or DataPart {type: agent-landscape/context}
        card.default_input_modes = ["text/plain", "text/markdown", "application/json"]
        return card

    async def survey(self, topic: Any, region_hint: str | None = None,
                     recall: list[dict[str, Any]] | None = None) -> Region:
        """Decide which topic region (area of the map) a new mission belongs to – or chart a new one.
        ``recall`` = related past missions (semantic search) – a strong hint for the right region."""
        rt = self.runtime
        hinted = rt.regions.get(region_hint or "")
        if hinted is not None and hinted.kind == "domain":
            return hinted
        domains = rt.domain_regions()
        infos = [{"id": r.id, "name": r.name, "biome": r.biome, "description": r.description,
                  "missions": [t.title for t in rt.region_missions(r.id)][-5:]} for r in domains]
        intel = rt.intel.get(topic.id)
        attached = ", ".join(str(e.get("name")) for e in (intel.summary if intel else [])[:12])
        related = [f"- {h.get('filename')} (region: {(h['store'].metadata or {}).get('region_name') or '?'}, "
                   f"score {h.get('score')})" for h in (recall or [])]
        raw = await self.think(
            survey_system(infos), f"Mission title: {topic.title}\nMission:\n{short(topic.goal, 2000)}"
            + (f"\nAttached context: {attached}" if attached else "")
            + ("\nRelated past missions (semantic search of the mission archive – prefer their region if the "
               "mission continues that work):\n" + "\n".join(related) if related else ""),
            activity=f"Surveying the landscape for '{short(topic.title, 32)}'", temperature=0.2, use_tools=False,
            model=rt.settings.orchestrator_model, mock=mock_survey(topic.title, topic.goal, infos))
        decision = parse_survey(raw)
        existing = rt.regions.get(str(decision.get("region_id") or ""))
        if existing is not None and existing.kind == "domain":
            return existing
        new = decision.get("new_region") if isinstance(decision.get("new_region"), dict) else {}
        name = " ".join(str(new.get("name") or topic.title).split())[:40] or "New Frontier"
        for r in domains:  # the model sometimes "creates" a region that already exists
            if r.name.lower() == name.lower():
                return r
        return rt.create_region(name, str(new.get("description") or topic.goal), str(new.get("biome") or ""))

    def _root_for(self, topic: Any, model: str | None, followup: bool, profile: str | None = None) -> Agent:
        """Feedback round → the mission's previous root; new mission → an idle veteran of the region; else spawn."""
        rt = self.runtime
        cls = SwarmLeader if topic.mode == "swarm" else Orchestrator
        veteran = None
        if followup:
            prev = rt.agents.get(topic.root_agent_id or "")
            if prev is not None and type(prev) is cls and rt.is_reusable(prev):
                veteran = prev
                model = model or prev.model
        if veteran is None:
            veteran = rt.find_veteran(cls, region_id=topic.region_id, parent=self, profile=profile)
        if veteran is not None:
            return rt.recruit(veteran, parent=self, topic_id=topic.id, model=model)
        return rt.spawn(cls, topic_id=topic.id, parent=self, depth=1, mode=topic.mode, model=model, profile=profile)

    async def propose(self, text: str, md: dict[str, Any]) -> str:
        """Operations report → topic + region check → pre-filled mission proposal (never starts it)."""
        rt = self.runtime
        msgs = [InboxMessage.from_dict(m) for m in md.get("ops_messages") or [] if isinstance(m, dict)]
        if not msgs:
            raise RuntimeError("Operations report without messages")
        triage = md.get("triage") or {}
        regions = [{"id": r.id, "name": r.name, "biome": r.biome, "description": r.description}
                   for r in rt.domain_regions()]
        recent = sorted((t for t in rt.topics.values() if t.kind == "mission"), key=lambda t: -t.created_at)[:15]
        missions = [{"id": t.id, "title": t.title, "status": t.status,
                     "region": rt.regions[t.region_id].name if t.region_id in rt.regions else "?"} for t in recent]
        self.set_status("delegating", "Reviewing an operations report")
        raw = await self.think(
            proposal_system(regions, missions), f"Intel note from the Communication Orchestrator:\n{text}\n\n"
            + "\n\n---\n\n".join(m.digest() for m in msgs),
            activity=f"Drafting a proposal: {short(msgs[0].subject or msgs[0].body, 32)}", temperature=0.2,
            use_tools=False, model=rt.settings.orchestrator_model, mock=mock_proposal(msgs, triage, regions))
        d = parse_proposal(raw, msgs, triage)
        follow = rt.topics.get(d["followup_topic_id"] or "")
        follow_ok = follow is not None and follow.kind == "mission" and follow.root_agent_id is not None
        region_id = d["region_id"] if d["region_id"] in rt.regions and rt.regions[d["region_id"]].kind == "domain" \
            else None
        if follow_ok:
            region_id = follow.region_id
        p = rt.ops.add_proposal(
            title=d["title"], goal=d["goal"], mode=d["mode"], priority=d["priority"], rationale=d["rationale"],
            note=str(md.get("ops_note") or text)[:4000], kind="followup" if follow_ok else "mission",
            followup_topic_id=follow.id if follow_ok else None, region_id=region_id,
            new_region=None if region_id else d["new_region"],
            channels=sorted({m.channel for m in msgs}), messages=[m.to_dict() for m in msgs])
        return (f"Proposal {p.id} '{p.title}' ({'feedback round' if follow_ok else 'new mission'}) is waiting for "
                "human approval.")

    async def run(self, text: str, ctx: TaskContext) -> str:
        md = ctx.metadata
        rt = self.runtime
        if md.get("intent") == "ops_report":  # from the operations team: propose, don't start
            return await self.propose(text, md)
        # A2A semantics: a message in an existing mission's contextId is feedback for that mission
        topic = rt.topics.get(str(md.get("topic_id") or "")) or rt.topics.get(ctx.task.context_id)
        if topic is not None and topic.kind != "mission":
            topic = None
        followup = topic is not None and topic.root_agent_id is not None
        if followup and topic.status == "active":
            raise RuntimeError(f"Mission '{topic.title}' is still running – send feedback once it has finished")
        if topic is None:
            topic = rt.create_topic(title=md.get("title") or short(text, 48), goal=text,
                                    mode=md.get("mode", "auto"), topic_id=ctx.task.context_id)
        topic.task_id = ctx.task.id
        if not followup and not topic.model_pool and (md.get("model") == "auto" or md.get("model_pool")):
            pool = md.get("model_pool") if isinstance(md.get("model_pool"), list) else ["*"]
            topic.model_pool = await rt.resolve_model_pool(pool)  # external A2A client asked for auto

        self.active_missions += 1
        self.set_status("delegating", f"Commanding {self.active_missions} mission(s)")
        try:
            items = await asyncio.to_thread(items_from_message, ctx.message)  # PDFs/Office are converted here
            if items:  # files (FilePart) + references (DataPart) attached to the A2A message
                self.set_status("delegating", f"Gathering intel ({len(items)} item{'s' if len(items) > 1 else ''})")
                await rt.attach_intel(topic, items)
            if followup:
                rt.reopen_topic(topic, text)
                prompt = (f"FEEDBACK / FOLLOW-UP (round {topic.rounds}):\n{text}\n\n"
                          f"Original mission:\n{short(topic.goal, 1500)}\n\n"
                          f"Your previous deliverable:\n{short(topic.result or '(none)', 4000)}\n\n"
                          "Revise and improve the deliverable so that it fully addresses the feedback.")
            else:
                recall = await rt.recall_past_missions(topic)  # all region archives, before routing
                if topic.center is None:
                    async with self._survey_lock:
                        rt.place_topic(topic, await self.survey(topic, md.get("region_id"), recall))
                await rt.link_region_knowledge(topic)
                rt.link_recalled_knowledge(topic, recall)
                briefing = recall_briefing(recall)
                prompt = f"{text}\n\n{briefing}" if briefing else text
            model = None if md.get("model") == "auto" or topic.model_pool else (md.get("model") or None)
            root = self._root_for(topic, model, followup, md.get("profile") or None)
            topic.root_agent_id = root.id
            rt.topic_changed(topic)
            result = await self.delegate(root, prompt, metadata={
                "topic_id": topic.id, "mode": topic.mode, "swarm_size": md.get("swarm_size"),
                "subtask": topic.title, "followup": followup})
            await rt.finalize_repos(topic)  # commit leftovers on mission branches
            rt.finish_topic(topic, "completed", result)
            rt.schedule_archive(topic)
            return result
        except asyncio.CancelledError:
            rt.finish_topic(topic, "canceled", None)
            raise
        except Exception as exc:
            rt.finish_topic(topic, "failed", str(exc))
            raise
        finally:
            self.active_missions -= 1
            if self.active_missions:
                self.set_status("delegating", f"Commanding {self.active_missions} mission(s)")


# ---------------------------------------------------------------- RemoteAgent
class RemoteAgent(Agent):
    """External A2A agent (e.g. one registered in LiteLLM's Agent Gateway) shown as an ally."""

    role = "ally"
    icon = "🤝"
    title = "Ally"
    persistent = True

    def __init__(self, *args: Any, remote_url: str, remote_card: AgentCard,
                 headers: dict[str, str] | None = None, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.remote_url = remote_url.rstrip("/")
        self.remote_card = remote_card
        self.headers = headers or {}
        self.description = remote_card.description
        self.title = f"Ally · {remote_card.name}"

    def brief(self) -> dict[str, str]:
        return {"id": self.id, "name": self.remote_card.name, "description": short(self.description, 200)}

    def card(self) -> AgentCard:
        card = self.remote_card.model_copy(deep=True)
        card.url = self.url  # proxied through this landscape
        return card

    async def run(self, text: str, ctx: TaskContext) -> str:
        self.set_status("working", f"Remote: {short(text, 60)}")
        client = A2AClient(self.remote_url, self.runtime.http, headers=self.headers)
        msg = Message.of(text, role="user", metadata=ctx.metadata or None)
        res = await client.send(msg, blocking=True)
        return result_text(res)

    def to_dict(self) -> dict[str, Any]:
        return {**super().to_dict(), "remote_url": self.remote_url}

"""OpsService: keeps the operations team alive (watch loops), stores the feed and mission proposals."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections import deque
from typing import TYPE_CHECKING, Any, Coroutine

from .proposals import Proposal
from .sources import CHANNELS, InboxMessage, MessageSource, MockSource, make_sources

if TYPE_CHECKING:
    from ..agents.ops import ChannelMonitor, CommsLead
    from ..runtime.runtime import Runtime

log = logging.getLogger(__name__)

OPS_TOPIC_ID = "ops"
LEAD_ID = "ops-comms"
FEED_MAX = 150
SEEN_MAX = 3000
PROPOSALS_MAX = 200


class OpsService:
    def __init__(self, runtime: "Runtime") -> None:
        self.rt = runtime
        self.kind = runtime.settings.ops_source
        self.error: str | None = None
        try:
            self.sources: dict[str, MessageSource] = make_sources(
                self.kind, interval=runtime.settings.ops_mock_interval, settings=runtime.settings)
        except ValueError as exc:
            self.sources, self.error = {}, str(exc)
            log.warning("Operations team disabled: %s", exc)
        self.proposals: dict[str, Proposal] = {}
        self.feed: deque[dict[str, Any]] = deque(maxlen=FEED_MAX)
        self.seen: deque[str] = deque(maxlen=SEEN_MAX)
        self._seen_set: set[str] = set()
        self.stats: dict[str, dict[str, Any]] = {}
        self.running = False
        self._tasks: set[asyncio.Task] = set()
        self._fetch_locks: dict[str, asyncio.Lock] = {}
        self._handle_locks: dict[str, asyncio.Lock] = {}

    def _lock(self, kind: dict[str, asyncio.Lock], channel: str) -> asyncio.Lock:
        return kind.setdefault(channel, asyncio.Lock())

    @property
    def enabled(self) -> bool:
        return bool(self.sources)

    # ------------------------------------------------------------- team
    @property
    def lead(self) -> "CommsLead | None":
        from ..agents.ops import CommsLead

        a = self.rt.agents.get(LEAD_ID)
        return a if isinstance(a, CommsLead) else None

    @property
    def monitors(self) -> list["ChannelMonitor"]:
        from ..agents.ops import ChannelMonitor

        return [a for a in self.rt.agents.values() if isinstance(a, ChannelMonitor)]

    def monitor_stats(self, agent_id: str) -> dict[str, Any]:
        return self.stats.setdefault(agent_id, {"seen": 0, "forwarded": 0, "errors": 0, "last_poll": None,
                                                "last_message": None})

    def _ensure_team(self) -> None:
        from ..agents.ops import ChannelMonitor, CommsLead
        from ..runtime.runtime import HQ_REGION_ID

        rt = self.rt
        if OPS_TOPIC_ID not in rt.topics:
            t = rt._new_topic("Operations Center", "Always-on, read-only monitoring of Outlook and Teams",
                              kind="ops", topic_id=OPS_TOPIC_ID)
            t.color, t.status = "#26c6da", "base"
            rt.place_topic(t, rt.regions[HQ_REGION_ID])
        lead = self.lead
        if lead is None:
            lead = rt.spawn(CommsLead, topic_id=OPS_TOPIC_ID, parent=rt.commander, name="Communication Orchestrator",
                            depth=1, agent_id=LEAD_ID)  # type: ignore[assignment]
            rt.log("📡 Operations team: Communication Orchestrator on duty", agent=lead, level="success")
        for channel in self.sources:
            if any(m.channel == channel for m in self.monitors):
                continue
            label = CHANNELS.get(channel, {}).get("label", channel)
            m = rt.spawn(ChannelMonitor, topic_id=OPS_TOPIC_ID, parent=lead, channel=channel, depth=2,
                         name=f"{label.split()[-1]} Monitor", agent_id=f"ops-{channel}")
            rt.log(f"👂 {m.name} is watching {label} (read-only)", agent=m, level="success")

    # -------------------------------------------------------- lifecycle
    @staticmethod
    def _is_mock_id(msg_id: str) -> bool:
        return bool(re.fullmatch(r"(outlook|teams)-[0-9a-f]{10}", msg_id or ""))

    def purge_mock(self) -> int:
        """Real source configured → forget simulated messages, proposals and counters from mock runs."""
        drop = [pid for pid, p in self.proposals.items()
                if p.messages and all(self._is_mock_id(m.get("id", "")) for m in p.messages)]
        for pid in drop:
            del self.proposals[pid]
        feed = [e for e in self.feed if not self._is_mock_id(e.get("id", ""))]
        removed = len(drop) + len(self.feed) - len(feed)
        if removed:
            self.feed.clear()
            self.feed.extend(feed)
            keep = [i for i in self.seen if not self._is_mock_id(i)]
            self.seen.clear()
            self.seen.extend(keep)
            self._seen_set = set(keep)
            self.stats = {}
            self.rt.persist.kv()
            log.info("Operations team: removed %d simulated (mock) proposals/messages", removed)
        return removed

    def start(self) -> None:
        if self.kind != "mock":
            self.purge_mock()
        if not self.enabled:
            for a in [self.lead, *self.monitors]:  # restored team, but monitoring is switched off
                if a is not None:
                    a.rest()
            return
        self._ensure_team()
        self.running = True
        for m in self.monitors:
            src = self.sources.get(m.channel)
            if src is not None:
                self.background(self._watch(m, src), name=f"ops-watch-{m.channel}")
            m.rest()
        if self.lead is not None:
            self.lead.rest()

    async def stop(self) -> None:
        self.running = False
        for t in list(self._tasks):
            t.cancel()
        for src in self.sources.values():
            with contextlib.suppress(Exception):
                await src.close()

    def background(self, coro: Coroutine, name: str | None = None) -> asyncio.Task:
        t = asyncio.create_task(coro, name=name)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    async def _watch(self, monitor: "ChannelMonitor", source: MessageSource) -> None:
        poll = self.rt.settings.ops_poll_seconds
        if isinstance(source, MockSource):
            poll = min(poll, 2.0)  # the mock is local – keep simulated messages snappy
        while True:
            try:
                await self._handle(monitor, await self._fetch(monitor, source))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 – a flaky source must not kill the monitor
                self.monitor_stats(monitor.id)["errors"] += 1
                self.rt.log(f"{monitor.name}: polling failed – {exc}", agent=monitor, level="warn")
            await asyncio.sleep(poll)

    async def _fetch(self, monitor: "ChannelMonitor", source: MessageSource) -> list[InboxMessage]:
        """New, not yet seen messages of one channel (one fetch at a time per channel)."""
        async with self._lock(self._fetch_locks, monitor.channel):
            msgs = await source.fetch()
        self.monitor_stats(monitor.id)["last_poll"] = time.time()
        return [m for m in msgs if m.id not in self._seen_set]

    async def _handle(self, monitor: "ChannelMonitor", msgs: list[InboxMessage]) -> None:
        async with self._lock(self._handle_locks, monitor.channel):  # triage in arrival order
            for msg in msgs:
                if msg.id not in self._seen_set:
                    await monitor.handle(msg)

    async def poll_now(self) -> dict[str, Any]:
        """Manual refresh: read every channel's source right away instead of waiting for the next poll.
        New messages are triaged in the background (results arrive live over the websocket)."""
        if not self.running:
            raise ValueError("The operations team is not running (OPS_SOURCE=off)")
        out: dict[str, Any] = {}
        for m in self.monitors:
            src = self.sources.get(m.channel)
            if src is None:
                continue
            try:
                msgs = await self._fetch(m, src)
            except Exception as exc:  # noqa: BLE001
                out[m.channel] = {"new": 0, "error": str(exc)}
                continue
            if msgs:
                self.background(self._handle(m, msgs), name=f"ops-refresh-{m.channel}")
            info = src.info() if hasattr(src, "info") else {}
            out[m.channel] = {"new": len(msgs), "files": info.get("files"), "waiting": info.get("waiting"),
                              "unreadable": len(info.get("errors") or [])}
            if m.status == "monitoring":
                m.rest()  # refresh the activity line ("… seen · … forwarded")
        self.rt.emit("ops_polled", result=out, snapshot=self.snapshot())
        return out

    # ------------------------------------------------------------- feed
    def record(self, monitor: "ChannelMonitor", msg: InboxMessage, decision: dict[str, Any]) -> dict[str, Any]:
        if msg.id not in self._seen_set:
            if len(self.seen) == self.seen.maxlen:
                self._seen_set.discard(self.seen[0])
            self.seen.append(msg.id)
            self._seen_set.add(msg.id)
        st = self.monitor_stats(monitor.id)
        st["seen"] += 1
        st["forwarded"] += 1 if decision.get("relevant") else 0
        st["last_message"] = time.time()
        entry = {**msg.brief(), "monitor_id": monitor.id, "relevant": bool(decision.get("relevant")),
                 "reason": decision.get("reason"), "summary": decision.get("summary"),
                 "priority": decision.get("priority", "normal"), "fallback": bool(decision.get("fallback")),
                 "ignored": bool(decision.get("ignored")),
                 "proposal_id": None}
        self.feed.append(entry)
        self.rt.emit("ops_message", entry=entry, stats=self.stats_brief())
        self.rt.persist.kv()
        return entry

    def _link_feed(self, msg_id: str, proposal_id: str) -> None:
        for e in self.feed:
            if e.get("id") == msg_id:
                e["proposal_id"] = proposal_id

    # -------------------------------------------------------- proposals
    def pending_for_thread(self, thread_id: str) -> Proposal | None:
        if not thread_id:
            return None
        return next((p for p in self.proposals.values() if p.status == "pending" and thread_id in p.thread_ids), None)

    def add_proposal(self, **kw: Any) -> Proposal:
        p = Proposal.new(**kw)
        self.proposals[p.id] = p
        while len(self.proposals) > PROPOSALS_MAX:  # forget the oldest decided ones
            old = next((k for k, v in self.proposals.items() if v.status != "pending"), None)
            if old is None:
                break
            del self.proposals[old]
        for m in p.messages:
            self._link_feed(m.get("id", ""), p.id)
        self._changed(p, created=True)
        where = (f"region '{self.rt.regions[p.region_id].name}'" if p.region_id in self.rt.regions
                 else f"new region '{p.new_region['name']}'" if p.new_region else "a region of HQ's choice")
        self.rt.log(f"📨 Mission proposal '{p.title}' ({where}) awaits your approval", level="success")
        return p

    def attach(self, p: Proposal, msg: InboxMessage) -> None:
        body = " ".join(msg.body.split())
        if any(m.get("id") == msg.id or " ".join(str(m.get("body", "")).split()) == body for m in p.messages):
            self._link_feed(msg.id, p.id)  # duplicate (re-sent / re-notified) – nothing new for the proposal
        else:
            p.messages.append(msg.to_dict())
            if msg.channel not in p.channels:
                p.channels.append(msg.channel)
            self._link_feed(msg.id, p.id)
            self._changed(p)

    def dismiss(self, proposal_id: str) -> Proposal | None:
        p = self.proposals.get(proposal_id)
        if p is None or p.status != "pending":
            return None
        p.status, p.decided_at = "dismissed", time.time()
        self._changed(p)
        return p

    def approved(self, proposal_id: str | None, topic_id: str) -> Proposal | None:
        p = self.proposals.get(proposal_id or "")
        if p is None or p.status != "pending":
            return None
        p.status, p.topic_id, p.decided_at = "approved", topic_id, time.time()
        self._changed(p)
        self.rt.log(f"✅ Proposal '{p.title}' approved – mission deployed", topic_id=topic_id, level="success")
        return p

    def _changed(self, p: Proposal, created: bool = False) -> None:
        p.updated_at = time.time()
        self.rt.emit("ops_proposal", proposal=p.to_dict(), created=created)
        self.rt.persist.kv()
        lead = self.lead
        if lead is not None and lead._inflight == 0:
            lead.rest()  # refresh "n proposals awaiting approval"

    # ---------------------------------------------------------- mock UI
    async def simulate(self, channel: str, *, relevant: bool | None = None, sender: str | None = None,
                       subject: str | None = None, body: str | None = None) -> dict[str, Any]:
        """Mock source only: a new message arrives right now."""
        src = self.sources.get(channel)
        if not isinstance(src, MockSource):
            raise ValueError("Simulated messages are only available with OPS_SOURCE=mock")
        msg = src.make(relevant, sender=sender, subject=subject, body=body)
        src.inject(msg)
        return msg.brief()

    # ------------------------------------------------------ persistence
    def stats_brief(self) -> dict[str, Any]:
        return {aid: dict(s) for aid, s in self.stats.items()}

    def dump(self) -> dict[str, Any]:
        return {"proposals": [p.record() for p in self.proposals.values()], "feed": list(self.feed),
                "seen": list(self.seen), "stats": self.stats}

    def restore(self, data: dict[str, Any] | None) -> None:
        if not data:
            return
        for d in data.get("proposals") or []:
            with contextlib.suppress(Exception):
                p = Proposal.from_dict(d)
                bodies: set[str] = set()
                unique = []
                for m in p.messages:
                    b = " ".join(str(m.get("body", "")).split())
                    if b not in bodies:
                        bodies.add(b)
                        unique.append(m)
                p.messages = unique
                self.proposals[p.id] = p
        self.feed.extend(data.get("feed") or [])
        for mid in data.get("seen") or []:
            self.seen.append(mid)
        self._seen_set = set(self.seen)
        self.stats = {k: dict(v) for k, v in (data.get("stats") or {}).items()}

    def snapshot(self) -> dict[str, Any]:
        lead = self.lead
        return {
            "enabled": self.enabled, "running": self.running, "source": self.kind, "error": self.error,
            "mock": any(isinstance(s, MockSource) for s in self.sources.values()),
            "folder": next((s.info()["root"] or f"⏳ waiting for {s.info()['waiting']}"
                            for s in self.sources.values() if hasattr(s, "info")), None),
            "lead_id": lead.id if lead else None, "topic_id": OPS_TOPIC_ID if OPS_TOPIC_ID in self.rt.topics else None,
            "monitors": [{"agent_id": m.id, "channel": m.channel, "label": m.label,
                          "icon": CHANNELS.get(m.channel, {}).get("icon", "📨"), **self.monitor_stats(m.id),
                          "source": self.sources[m.channel].info() if hasattr(self.sources.get(m.channel), "info")
                          else None}
                         for m in self.monitors],
            "proposals": [p.to_dict() for p in sorted(self.proposals.values(), key=lambda p: -p.created_at)][:60],
            "feed": list(self.feed)[-60:],
        }

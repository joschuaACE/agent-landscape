"""Operations team units: always-on channel monitors and their Communication Orchestrator.

They are not started by the Commander – the runtime keeps them running. Monitors read their channel
(read-only), triage each new message and forward relevant ones via A2A to the Communication
Orchestrator, which writes an intel note for the Commander. The Commander then prepares a mission
proposal that waits for human approval.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from ..a2a.task_manager import TaskContext
from ..ops.proposals import TRIAGE_FORMAT, heuristic_triage, parse_triage
from ..ops.sources import CHANNELS, InboxMessage
from .base import Agent, short


class OpsAgent(Agent):
    """Persistent unit that rests in 'monitoring' (not 'idle') between jobs."""

    persistent = True
    profile_kind = "monitor"
    default_profile = "outlook-monitor"

    def __init__(self, runtime: Any, *, profile: str | None = None, **kw: Any) -> None:
        p = runtime.profiles.resolve(profile, self.profile_kind, self.default_profile)
        self.profile_key, self._snapshot = p.key, p
        self.icon, self.title, self.description = p.icon, p.title, p.description or self.description
        super().__init__(runtime, **kw)

    def _prof(self) -> Any:
        return self.profile or self._snapshot

    def rest_activity(self) -> str:
        return "Standing by"

    def rest(self) -> None:
        if self._inflight == 0:
            online = self.runtime.ops.running
            self.set_status("monitoring" if online else "idle",
                            self.rest_activity() if online else "Offline (OPS_SOURCE=off)")


class ChannelMonitor(OpsAgent):
    role = "monitor"
    description = "Watches one communication channel (read-only) and forwards relevant messages."

    def __init__(self, runtime: Any, *, channel: str = "outlook", profile: str | None = None, **kw: Any) -> None:
        self.channel = channel
        self.default_profile = f"{channel}-monitor"
        super().__init__(runtime, profile=profile, **kw)
        self.options["channel"] = channel  # persisted → restored with the same channel
        self._forwarding = 0

    @property
    def label(self) -> str:
        return CHANNELS.get(self.channel, {}).get("label", self.channel)

    def rest_activity(self) -> str:
        st = self.runtime.ops.monitor_stats(self.id)
        return f"👂 Watching {self.label} · {st['seen']} seen · {st['forwarded']} forwarded"

    def rest(self) -> None:
        if self._forwarding == 0:
            super().rest()

    async def run(self, text: str, ctx: TaskContext) -> str:
        # direct A2A question to a monitor: report what it saw (still read-only)
        st = self.runtime.ops.monitor_stats(self.id)
        recent = [e for e in self.runtime.ops.feed if e.get("monitor_id") == self.id][-5:]
        lines = "\n".join(f"- [{'relevant' if e.get('relevant') else 'noise'}] {e.get('sender')}: "
                          f"{e.get('subject') or e.get('preview')}" for e in recent)
        return f"{self.label}: {st['seen']} messages seen, {st['forwarded']} forwarded.\n{lines}".strip()

    async def triage(self, msg: InboxMessage) -> dict[str, Any]:
        prof = self._prof()
        raw = await self.think(prof.prompt + TRIAGE_FORMAT, msg.digest(),
                               activity=f"Triaging: {short(msg.subject or msg.body, 40)}", temperature=0.1,
                               use_tools=False, mock=json.dumps(heuristic_triage(msg)))
        return parse_triage(raw, msg)

    async def handle(self, msg: InboxMessage) -> dict[str, Any]:
        """Triage one new message; relevant ones go to the Communication Orchestrator (in the background)."""
        rt = self.runtime
        self.last_input = msg.digest(600)
        if msg.meta.get("ignored"):  # configured filter – no LLM call, but visible in the feed
            entry = rt.ops.record(self, msg, {"relevant": False, "reason": f"Ignored: {msg.meta['ignored']}",
                                              "summary": "", "priority": "low", "ignored": True})
            self.rest()
            return entry
        try:
            decision = await self.triage(msg)
        except Exception as exc:  # noqa: BLE001 – LLM down → keyword triage, keep watching
            rt.log(f"{self.name}: triage failed ({exc}) – using keyword rules", agent=self, level="warn")
            decision = {**heuristic_triage(msg), "fallback": True}
        entry = rt.ops.record(self, msg, decision)
        self.gain_xp(5)
        lead = rt.ops.lead
        if decision["relevant"] and lead is not None:
            self._forwarding += 1
            self.set_status("delegating", f"📨 Forwarding: {short(msg.subject or msg.body, 40)}")
            rt.ops.background(self._forward(lead, msg, decision))
        else:
            self.rest()
        return entry

    async def _forward(self, lead: Agent, msg: InboxMessage, decision: dict[str, Any]) -> None:
        try:
            await self.delegate(lead, f"Relevant {self.label} message ({decision.get('priority', 'normal')} "
                                f"priority): {decision.get('summary')}\nWhy: {decision.get('reason')}\n\n"
                                f"{msg.digest()}",
                                metadata={"intent": "ops_signal", "ops_message": msg.to_dict(), "triage": decision,
                                          "subtask": f"Signal: {short(msg.subject or msg.body, 50)}"})
            self.tasks_done += 1
            self.last_output = f"Forwarded to {lead.name}: {decision.get('summary')}"
        except Exception as exc:  # noqa: BLE001
            self.runtime.log(f"{self.name}: forwarding failed – {exc}", agent=self, level="warn")
        finally:
            self._forwarding -= 1
            self.rest()


class CommsLead(OpsAgent):
    role = "ops_lead"
    profile_kind = "ops_lead"
    default_profile = "comms-orchestrator"
    description = "Leads the operations team: consolidates signals from the monitors and briefs the Commander."

    def rest_activity(self) -> str:
        pending = sum(1 for p in self.runtime.ops.proposals.values() if p.status == "pending")
        return f"📡 Coordinating {len(self.runtime.ops.monitors)} monitors" + (
            f" · {pending} proposal{'s' if pending != 1 else ''} awaiting approval" if pending else "")

    async def run(self, text: str, ctx: TaskContext) -> str:
        rt, md = self.runtime, ctx.metadata
        if md.get("intent") != "ops_signal" or not isinstance(md.get("ops_message"), dict):
            return self.rest_activity()
        msg = InboxMessage.from_dict(md["ops_message"])
        triage = md.get("triage") or {}
        # same conversation as a pending proposal → enrich it instead of bothering the Commander again
        existing = rt.ops.pending_for_thread(msg.thread_id)
        if existing is not None:
            rt.ops.attach(existing, msg)
            return f"Added to pending proposal '{existing.title}' ({existing.id})."
        ch = CHANNELS.get(msg.channel, {}).get("label", msg.channel)
        note = await self.think(
            self._prof().prompt, f"Signal from the {ch} monitor:\n\n{text}",
            activity=f"Assessing: {short(msg.subject or msg.body, 40)}", temperature=0.2, use_tools=False,
            mock=(f"**Signal ({triage.get('priority', 'normal')} priority, {ch}):** {triage.get('summary')}\n\n"
                  f"**Why it matters:** {triage.get('reason')}\n\n**Requested by:** {msg.sender}"))
        commander = rt.commander
        if commander is None:
            raise RuntimeError("Commander not available")
        await asyncio.sleep(0)  # let the UI show the hand-over
        return await self.delegate(commander, note, metadata={
            "intent": "ops_report", "ops_messages": [msg.to_dict()], "triage": triage, "ops_note": note,
            "subtask": f"Proposal: {short(msg.subject or msg.body, 50)}"})

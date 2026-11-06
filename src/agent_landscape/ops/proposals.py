"""Triage + mission proposals of the operations team (prompts, parsing, mock answers)."""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from ..runtime.regions import BIOMES, DOMAIN_BIOMES, guess_biome, mock_survey, parse_survey
from .sources import CHANNELS, InboxMessage

PRIORITIES = ("low", "normal", "high")


# ------------------------------------------------------------------ triage
TRIAGE_FORMAT = (
    "\n\n---\nAssess the message below. Respond ONLY with JSON:\n"
    '{"relevant": true|false, "reason": "<why, max 20 words>", "summary": "<one sentence: who needs what by '
    'when>", "priority": "low|normal|high"}'
)

_ASK = re.compile(r"\b(can you|could you|can someone|can we|could we|please|need|needs|request|urgent|asap|"
                  r"deadline|until|by (monday|tuesday|wednesday|thursday|friday|next week|eod)|review|assess|"
                  r"propose|draft|prepare|offer|who can|anyone)\b", re.I)
_NOISE = re.compile(r"\b(newsletter|digest|unsubscribe|accepted|declined|has been closed|lunch|birthday|menu|"
                    r"out of office|moved to done)\b", re.I)


def heuristic_triage(msg: InboxMessage) -> dict[str, Any]:
    """Keyword triage – mock mode and fallback when the LLM answer can't be parsed."""
    text = f"{msg.subject}\n{msg.body}"
    hinted = msg.meta.get("mock_relevant")
    relevant = bool(hinted) if hinted is not None else bool(_ASK.search(text)) and not _NOISE.search(text)
    high = msg.importance == "high" or bool(re.search(r"\b(urgent|asap|blocks?|go-live|outage)\b", text, re.I))
    first = " ".join((msg.subject or msg.body).split())[:140]
    return {"relevant": relevant,
            "reason": ("Concrete request with an expected deliverable" if relevant
                       else "Informational / no action for the team"),
            "summary": f"{msg.sender.split('<')[0].strip()}: {first}",
            "priority": "high" if relevant and high else "normal" if relevant else "low"}


def _json(raw: str) -> dict[str, Any]:
    return parse_survey(raw)  # first {...} block, tolerant


def parse_triage(raw: str, msg: InboxMessage) -> dict[str, Any]:
    data = _json(raw)
    if "relevant" not in data:
        return {**heuristic_triage(msg), "fallback": True}
    rel = data.get("relevant")
    rel = rel.strip().lower() in ("true", "yes", "1") if isinstance(rel, str) else bool(rel)
    prio = str(data.get("priority") or "normal").lower()
    return {"relevant": rel, "reason": " ".join(str(data.get("reason") or "").split())[:200],
            "summary": " ".join(str(data.get("summary") or msg.subject or msg.body).split())[:300],
            "priority": prio if prio in PRIORITIES else "normal"}


# --------------------------------------------------------------- proposals
@dataclass
class Proposal:
    id: str
    title: str
    goal: str
    mode: str = "auto"
    kind: str = "mission"  # mission | followup (feedback round for an existing mission)
    status: str = "pending"  # pending | approved | dismissed
    priority: str = "normal"
    rationale: str = ""
    note: str = ""  # intel note of the Communication Orchestrator
    region_id: str | None = None
    new_region: dict[str, str] | None = None  # {name, description, biome} – charted only on approval
    followup_topic_id: str | None = None
    channels: list[str] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)  # InboxMessage.to_dict()
    topic_id: str | None = None  # mission created from it
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    decided_at: float | None = None

    @classmethod
    def new(cls, **kw: Any) -> "Proposal":
        return cls(id=f"prop-{uuid.uuid4().hex[:8]}", **kw)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Proposal":
        names = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in names})

    @property
    def thread_ids(self) -> set[str]:
        return {m.get("thread_id") for m in self.messages if m.get("thread_id")}

    def intel_items(self) -> list[dict[str, Any]]:
        """Source messages as briefing attachments (composer context items)."""
        out = []
        for i, m in enumerate(self.messages, 1):
            msg = InboxMessage.from_dict(m)
            slug = re.sub(r"[^a-z0-9]+", "-", (msg.subject or msg.sender).lower()).strip("-")[:40] or "message"
            out.append({"kind": "file", "name": f"{msg.channel}-{i}-{slug}.md", "content": msg.markdown(),
                        "mime": "text/markdown"})
        return out

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["messages"] = [InboxMessage.from_dict(m).brief() for m in self.messages]
        d["intel"] = self.intel_items()
        return d

    def record(self) -> dict[str, Any]:
        return asdict(self)


def proposal_system(regions: list[dict[str, Any]], missions: list[dict[str, Any]]) -> str:
    lines = "\n".join(f'- id="{r["id"]}" name="{r["name"]}" ({r["biome"]}): {r["description"]}' for r in regions) \
        or "(none yet)"
    mlines = "\n".join(f'- id="{m["id"]}" "{m["title"]}" [{m["status"]}] in region {m["region"]}' for m in missions) \
        or "(none)"
    biomes = "\n".join(f"- {k}: {BIOMES[k].label} – {BIOMES[k].hint}" for k in DOMAIN_BIOMES)
    return (
        "You are the High Commander of an agent landscape. Your read-only operations team monitors Outlook and "
        "Teams and just reported a relevant message. You do NOT start anything yourself: you prepare a mission "
        "proposal that a human approves.\n\n"
        "1. Topic check: if the message continues one of the recent missions below (same deliverable, follow-up "
        "question, correction), propose a FEEDBACK round for that mission (followup_topic_id). Otherwise a new "
        "mission.\n"
        "2. Region check: pick the existing region of the same product / project / domain, or describe a new "
        "region.\n"
        "3. Write the mission briefing for the agent team: objective, relevant context from the message, the "
        "expected deliverable and any deadline. Never instruct agents to reply to or contact anyone – the human "
        "decides about communication.\n\n"
        f"Existing regions:\n{lines}\n\nRecent missions:\n{mlines}\n\nBiomes for new regions:\n{biomes}\n\n"
        "Respond ONLY with JSON:\n"
        '{"title": "<max 6 words>", "goal": "<mission briefing, markdown>", "mode": "auto|hierarchy|swarm", '
        '"priority": "low|normal|high", "rationale": "<one sentence why this deserves a mission>", '
        '"followup_topic_id": "<mission id or null>", "region_id": "<existing region id or null>", '
        '"new_region": {"name": "<2-4 words>", "description": "<one sentence>", "biome": "<biome key>"} or null}'
    )


def mock_proposal(msgs: list[InboxMessage], triage: dict[str, Any], regions: list[dict[str, Any]]) -> str:
    m = msgs[0]
    raw = re.sub(r"^(re|fw|fwd|aw|wg|urgent|request|offer request):\s*", "", m.subject or "", flags=re.I)
    title = " ".join((raw or m.body).replace("@team", "").split()[:6]).strip(" ,.:-") or "Incoming request"
    ch = CHANNELS.get(m.channel, {}).get("label", m.channel)
    goal = (f"{triage.get('summary') or title}\n\n**Source:** {ch} – {m.sender}"
            + (f" ({m.location})" if m.location else "") + "\n\n"
            "**Objective:** Work out what is being asked and prepare the requested deliverable.\n\n"
            "**Deliverable:** A concise, ready-to-review document that answers the request (the human decides "
            "how and whether to reply).\n\n> " + " ".join(m.body.split())[:600])
    survey = json.loads(mock_survey(title, f"{m.subject} {m.body}", [{**r, "missions": []} for r in regions]))
    return json.dumps({"title": title, "goal": goal, "mode": "auto", "priority": triage.get("priority", "normal"),
                       "rationale": triage.get("reason") or "Concrete request", "followup_topic_id": None,
                       "region_id": survey.get("region_id"), "new_region": survey.get("new_region")})


def parse_proposal(raw: str, msgs: list[InboxMessage], triage: dict[str, Any]) -> dict[str, Any]:
    data = _json(raw)
    m = msgs[0]
    title = " ".join(str(data.get("title") or m.subject or m.body).split())[:80] or "Incoming request"
    goal = str(data.get("goal") or "").strip() or (f"{triage.get('summary') or title}\n\n" + m.digest())
    mode = str(data.get("mode") or "auto").lower()
    prio = str(data.get("priority") or triage.get("priority") or "normal").lower()
    new_region = data.get("new_region") if isinstance(data.get("new_region"), dict) else None
    if new_region is not None:
        name = " ".join(str(new_region.get("name") or title).split())[:40]
        desc = " ".join(str(new_region.get("description") or title).split())[:200]
        biome = str(new_region.get("biome") or "")
        new_region = {"name": name, "description": desc,
                      "biome": biome if biome in DOMAIN_BIOMES else guess_biome(f"{name} {desc}")}
    return {"title": title, "goal": goal[:8000], "mode": mode if mode in ("auto", "hierarchy", "swarm") else "auto",
            "priority": prio if prio in PRIORITIES else "normal",
            "rationale": " ".join(str(data.get("rationale") or triage.get("reason") or "").split())[:300],
            "followup_topic_id": str(data.get("followup_topic_id") or "") or None,
            "region_id": str(data.get("region_id") or "") or None, "new_region": new_region}

"""Message sources for the operations team – strictly read-only.

A source exposes exactly one operation, :meth:`MessageSource.fetch` (new messages since the last call).
There is deliberately no send / reply / mark-as-read / move: monitors cannot change anything in the
mailbox or in Teams.

Available sources:

* ``mock`` – a simulated Outlook inbox and Teams feed (demo / until Microsoft Graph access is approved).

Planned: ``graph`` – Microsoft Graph polling with delegated, read-only scopes
(``Mail.Read``, ``Chat.Read``, ``ChannelMessage.Read.All``).
"""

from __future__ import annotations

import random
import re
import time
import uuid
import zlib
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any

CHANNELS: dict[str, dict[str, str]] = {
    "outlook": {"label": "Outlook", "icon": "📧"},
    "teams": {"label": "Microsoft Teams", "icon": "💬"},
}


@dataclass
class InboxMessage:
    id: str
    channel: str  # outlook | teams
    sender: str
    subject: str
    body: str
    location: str = ""  # "Inbox", "Chat · Jonas Becker", "AI Platform › #architecture"
    thread_id: str = ""
    received_at: float = field(default_factory=time.time)
    importance: str = "normal"
    link: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)  # source specific (never shown to the LLM)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "InboxMessage":
        names = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in names})

    def brief(self) -> dict[str, Any]:
        return {"id": self.id, "channel": self.channel, "sender": self.sender, "subject": self.subject,
                "location": self.location, "thread_id": self.thread_id, "received_at": self.received_at,
                "importance": self.importance, "preview": " ".join(self.body.split())[:220]}

    def digest(self, limit: int = 2500) -> str:
        """Plain-text view for the agents."""
        ch = CHANNELS.get(self.channel, {}).get("label", self.channel)
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.received_at))
        head = (f"Channel: {ch}" + (f" · {self.location}" if self.location else "") + f"\nFrom: {self.sender}\n"
                f"Received: {when}" + (f"\nImportance: {self.importance}" if self.importance != "normal" else ""))
        if self.subject:
            head += f"\nSubject: {self.subject}"
        body = self.body if len(self.body) <= limit else self.body[: limit - 1] + "…"
        return f"{head}\n\n{body}"

    def markdown(self) -> str:
        """Attachment for the mission briefing (intel file)."""
        return f"# {self.subject or 'Message'}\n\n```\n{self.digest(20000)}\n```\n"


class MessageSource:
    """Read-only message feed of one channel."""

    kind = "base"

    def __init__(self, channel: str) -> None:
        self.channel = channel
        self.label = CHANNELS.get(channel, {}).get("label", channel)

    async def fetch(self) -> list[InboxMessage]:  # pragma: no cover - interface
        raise NotImplementedError

    async def close(self) -> None:
        return None


# ------------------------------------------------------------------- mock
# (sender, location, subject, body, relevant)
_OUTLOOK = [
    ("Lena Vogel <lena.vogel@contoso.com>", "Inbox", "Request: vector database comparison for the RAG pilot",
     "Hi,\n\ncould you prepare a short comparison of pgvector, Qdrant and Azure AI Search for our RAG pilot? "
     "We need a recommendation incl. a rough cost estimate by Friday for the steering committee.\n\nThanks, Lena",
     True),
    ("Markus Weber <markus.weber@contoso.com>", "Inbox", "URGENT: pentest finding in the Copilot Studio connector",
     "Hi team,\n\nthe pentest report flagged that our custom Copilot Studio connector stores the API key in plain "
     "text in the environment variables. Please assess the risk and propose a fix before the go-live next "
     "Tuesday.\n\nMarkus", True),
    ("Julia Hoffmann <j.hoffmann@fabrikam.com>", "Inbox", "Offer request: Teams governance workshop",
     "Dear all,\n\nwe would like to receive an offer for a 2-day Microsoft Teams governance workshop – agenda, "
     "deliverables and an effort estimate. Could you send us something by next week?\n\nBest regards, Julia "
     "Hoffmann, Fabrikam", True),
    ("Thomas Brandt <thomas.brandt@contoso.com>", "Inbox", "Re: ModFac migration – risk assessment needed",
     "Hi,\n\nbefore we freeze the scope of the ModFac legacy migration we need a risk assessment of the batch "
     "job migration (dependencies, cut-over, rollback). Can the AI team draft one until Thursday?\n\nThomas",
     True),
    ("AI Weekly <news@aiweekly.example>", "Inbox", "Your weekly digest: 5 AI trends you missed",
     "This week: agents everywhere, new open-weight models and more. Unsubscribe here.", False),
    ("Microsoft Outlook", "Inbox", "Accepted: Jour fixe AI Platform",
     "Anna Schmidt has accepted this meeting.", False),
    ("IT Service Desk <servicedesk@contoso.com>", "Inbox", "Ticket #48213 has been closed",
     "Your ticket 'Docking station not working' has been resolved and closed.", False),
    ("Canteen <canteen@contoso.com>", "Inbox", "Menu of the week", "Monday: pasta, Tuesday: curry …", False),
]
_TEAMS = [
    ("Jonas Becker", "Chat · Jonas Becker", "",
     "hey, the LiteLLM proxy keeps returning 429s since this morning – can someone look into the rate limits "
     "and propose a config fix? it blocks the whole team", True),
    ("Sophie Klein", "AI Platform › #architecture", "",
     "Can we get an ADR draft for moving our agent runtime from polling to event-driven (Service Bus vs. Event "
     "Grid)? I want to discuss it in Thursday's architecture board.", True),
    ("Tim Richter", "ModFac › #migration", "",
     "@team we need an assessment sheet for the SDD BWA service before the customer call on Monday – who can "
     "prepare a first draft based on the offer guideline?", True),
    ("Anna Schmidt", "Chat · Anna Schmidt", "", "lunch at 12? 🍜", False),
    ("Praise Bot", "Company › #general", "", "🎉 Happy birthday Max!", False),
    ("Sophie Klein", "AI Platform › #architecture", "", "👍 thanks!", False),
    ("Planner", "AI Platform › #general", "", "Task 'Update team slides' was moved to Done.", False),
]
_POOLS = {"outlook": _OUTLOOK, "teams": _TEAMS}


def _thread(channel: str, location: str, subject: str, body: str) -> str:
    base = re.sub(r"^(re|fw|fwd|aw|wg):\s*", "", (subject or body[:60]).lower()).strip()
    return f"{channel}-{zlib.crc32(f'{location}|{base}'.encode()):08x}"


class MockSource(MessageSource):
    """Simulated feed: a new message roughly every ``interval`` seconds (~45 % of them relevant)."""

    kind = "mock"

    def __init__(self, channel: str, *, interval: float = 120.0, first_after: float | None = None,
                 seed: int | None = None) -> None:
        super().__init__(channel)
        self.interval = interval
        self.rng = random.Random(seed)
        self.pool = _POOLS.get(channel, _OUTLOOK)
        self._recent: deque[int] = deque(maxlen=max(1, len(self.pool) - 2))  # no quick repeats
        self._queue: deque[InboxMessage] = deque()
        delay = first_after if first_after is not None else self.rng.uniform(0.2, 0.5) * interval
        self._next = time.time() + delay

    def make(self, relevant: bool | None = None, *, sender: str | None = None, subject: str | None = None,
             body: str | None = None, location: str | None = None) -> InboxMessage:
        """A message from the pool (optionally only relevant / only noise), or a custom one."""
        if body:
            snd, loc, subj, txt, rel = (sender or "Simulated sender", location or ("Inbox" if self.channel ==
                                        "outlook" else "Chat"), subject or "", body, None)
        else:
            idx = [i for i, m in enumerate(self.pool) if relevant is None or m[4] == relevant] or list(range(len(self.pool)))
            fresh = [i for i in idx if i not in self._recent] or idx
            pick = self.rng.choice(fresh)
            self._recent.append(pick)
            snd, loc, subj, txt, rel = self.pool[pick]
        return InboxMessage(
            id=f"{self.channel}-{uuid.uuid4().hex[:10]}", channel=self.channel, sender=snd, subject=subj,
            body=txt, location=loc, thread_id=_thread(self.channel, loc, subj, txt),
            importance="high" if subj.upper().startswith("URGENT") else "normal",
            meta={"mock_relevant": rel} if rel is not None else {})

    def inject(self, msg: InboxMessage) -> None:
        self._queue.append(msg)

    async def fetch(self) -> list[InboxMessage]:
        out = list(self._queue)
        self._queue.clear()
        now = time.time()
        if now >= self._next:
            out.append(self.make())
            self._next = now + self.rng.uniform(0.5, 1.5) * self.interval
        return out


def make_sources(kind: str, *, interval: float = 120.0, settings: Any = None) -> dict[str, MessageSource]:
    kind = (kind or "off").lower()
    if kind == "mock":
        return {ch: MockSource(ch, interval=interval) for ch in CHANNELS}
    if kind == "folder":
        from .folder import FolderSource, find_inbox_dir

        configured = getattr(settings, "ops_inbox_dir", None)
        root = find_inbox_dir(configured)  # None → the sources keep looking until OneDrive has synced it
        return {ch: FolderSource(ch, root, configured=configured,
                                 backlog_hours=getattr(settings, "ops_backlog_hours", 24.0),
                                 ignore_senders=getattr(settings, "ops_ignore_senders", []))
                for ch in CHANNELS}
    if kind in ("off", "none", "false", "0", ""):
        return {}
    raise ValueError(f"Unknown OPS_SOURCE '{kind}' – supported: folder, mock, off")

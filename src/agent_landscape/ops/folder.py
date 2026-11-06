"""Folder source: JSON messages that Power Automate flows write into a (OneDrive-synced) folder.

    AgentInbox/
    ├── mail/              agentinbox.mail.v1   (Outlook – "When a new email arrives (V3)")
    └── teams/
        ├── channels/      agentinbox.teams.v1  (kind = channel)
        └── chats/         agentinbox.teams.v1  (kind = chat)

Strictly read-only: files are only opened for reading – never moved, renamed or deleted (retention is the
cleanup flow's job). Half-synced / invalid files are retried on the next poll.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any

from .sources import InboxMessage, MessageSource

log = logging.getLogger(__name__)

SUBDIRS = {"outlook": ("mail",), "teams": ("teams/channels", "teams/chats", "teams")}
_TS_KEYS = ("receivedDateTime", "createdDateTime", "capturedAtUtc")


def find_inbox_dir(configured: str | None) -> Path | None:
    """OPS_INBOX_DIR, else the first OneDrive*/AgentInbox (macOS CloudStorage or Windows OneDrive)."""
    if configured:
        p = Path(configured).expanduser()
        return p if p.is_dir() else None
    candidates = glob.glob(os.path.expanduser("~/Library/CloudStorage/OneDrive*/AgentInbox"))
    for env in ("OneDriveCommercial", "OneDrive"):
        if os.getenv(env):
            candidates.append(os.path.join(os.environ[env], "AgentInbox"))
    candidates += glob.glob(os.path.expanduser("~/OneDrive*/AgentInbox"))
    return next((Path(c) for c in candidates if Path(c).is_dir()), None)


def _ts(d: dict[str, Any], fallback: float) -> float:
    for k in _TS_KEYS:
        v = d.get(k)
        if isinstance(v, str) and v and not v.startswith("@"):
            v = re.sub(r"(\.\d{6})\d+", r"\1", v.strip().replace("Z", "+00:00"))
            try:
                t = datetime.fromisoformat(v)
                return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp()
            except ValueError:
                continue
    return fallback


def _field(v: Any) -> str:
    """Header-like value as-is (ids, addresses – may contain '<…>'); drops unconverted Power Automate
    expressions ('@{…}' / '@outputs(…)' = the Compose paste wasn't turned into expressions)."""
    if v is None or isinstance(v, (dict, list)) and not v:
        return ""
    s = (v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)).strip()
    return "" if re.fullmatch(r"@@?[{a-zA-Z].*", s, re.S) else s


def _body(v: Any) -> str:
    """Message body → plain text (Teams bodies are HTML even after 'Html to text' was skipped)."""
    s = _field(v)
    if not s:
        return ""
    s = re.sub(r"<(br|/p|/div|/li)\s*/?>", "\n", s, flags=re.I)
    s = unescape(re.sub(r"</?[a-zA-Z][a-zA-Z0-9]*(\s[^<>]*)?/?>", " ", s))  # tags only, keeps <a@b.com>
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", s)).strip()


def _sender(v: Any) -> str:
    if isinstance(v, dict):  # {"emailAddress": {"name", "address"}} or {"user": {"displayName"}}
        e = v.get("emailAddress") or v.get("user") or v
        name, addr = e.get("name") or e.get("displayName"), e.get("address")
        return f"{name} <{addr}>" if name and addr else str(name or addr or "")
    return _field(v)


def parse_file(path: Path, channel: str, d: dict[str, Any]) -> InboxMessage | None:
    """One JSON file (schemas agentinbox.mail.v1 / agentinbox.teams.v1, tolerant to variations) → message."""
    mtime = path.stat().st_mtime
    if channel == "outlook":
        key = _field(d.get("internetMessageId")) or _field(d.get("id")) or path.name
        body = _body(d.get("bodyText")) or _body(d.get("body")) or _body(d.get("bodyPreview"))
        imp = (_field(d.get("importance")) or "normal").lower()
        return InboxMessage(
            id=f"outlook:{key}", channel="outlook", sender=_sender(d.get("from")) or "unknown sender",
            subject=_field(d.get("subject")), body=body or "[no readable body – possibly protected, open in Outlook]",
            location=_field(d.get("folder")) or "Inbox", thread_id=f"outlook-{_field(d.get('conversationId')) or key}",
            received_at=_ts(d, mtime), importance="high" if imp == "high" else "normal",
            link=_field(d.get("webLink")) or None,
            meta={"file": path.name, "to": _field(d.get("toRecipients")), "cc": _field(d.get("ccRecipients")),
                  "has_attachments": d.get("hasAttachments")})
    mtype = _field(d.get("messageType")).lower()
    if mtype and mtype != "message":  # system events (member added, renamed …)
        return None
    kind = _field(d.get("kind")) or ("chat" if _field(d.get("chatId")) else "channel")
    msg_id = _field(d.get("messageId")) or _field(d.get("id")) or path.stem
    if kind == "chat":
        where = _field(d.get("chatName")) or _field(d.get("chatTopic")) or "Chat"
        thread = _field(d.get("chatId")) or msg_id
        location = where if where.lower().startswith("chat") else f"Chat · {where}"
    else:
        team, chan = _field(d.get("teamName")), _field(d.get("channelName"))
        location = f"{team} › #{chan}" if team and chan else f"#{chan}" if chan else "Teams channel"
        thread = f"{_field(d.get('channelId'))}:{_field(d.get('replyToId')) or msg_id}"
    sender = _field(d.get("fromDisplayName")) or _sender(d.get("from")) or "unknown"
    imp = (_field(d.get("importance")) or "normal").lower()
    return InboxMessage(
        id=f"teams:{_field(d.get('channelId')) or _field(d.get('chatId'))}:{msg_id}", channel="teams", sender=sender,
        subject=_field(d.get("subject")), body=_body(d.get("bodyText")) or _body(d.get("body")) or "[no readable text]",
        location=location, thread_id=f"teams-{thread}", received_at=_ts(d, mtime),
        importance="high" if imp in ("high", "urgent") else "normal", link=_field(d.get("webUrl")) or None,
        meta={"file": path.name, "kind": kind, "is_from_app": bool(d.get("isFromApp")),
              "reply_to": _field(d.get("replyToId")) or None})


class FolderSource(MessageSource):
    """Polls ``<root>/<subdir>/*.json`` – read-only."""

    kind = "folder"

    def __init__(self, channel: str, root: Path | None, *, configured: str | None = None,
                 backlog_hours: float = 24.0, ignore_senders: list[str] | None = None) -> None:
        super().__init__(channel)
        self.configured = configured
        self.root: Path | None = None
        self.dirs: list[Path] = []
        self._set_root(root)
        self.ignore = [s.lower() for s in ignore_senders or []]
        self.not_before = time.time() - backlog_hours * 3600
        self._done: dict[str, float] = {}  # path → mtime already handled (or skipped)
        self.files = 0
        self.errors: list[str] = []

    def _set_root(self, root: Path | None) -> None:
        self.root = root
        self.dirs = [root / s for s in SUBDIRS.get(self.channel, ())] if root else []

    def _paths(self) -> list[Path]:
        out: dict[str, Path] = {}
        for d in self.dirs:
            if d.is_dir():  # non-recursive: teams/ itself only holds files if the user didn't split by kind
                for p in d.glob("*.json"):
                    out[str(p)] = p
        return sorted(out.values(), key=lambda p: p.name)

    def _ignored(self, sender: str) -> bool:
        """'Joschua Brixner' also matches 'joschua.brixner@…' (and vice versa)."""
        norm = lambda s: re.sub(r"[^a-z0-9@]+", " ", s.lower()).strip()  # noqa: E731
        who = norm(sender)
        return any(norm(s) and norm(s) in who for s in self.ignore)

    @staticmethod
    def _diagnose(p: Path, exc: Exception) -> str:
        """Half-synced file (retry) vs. a flow that writes the wrong content (fix the flow)."""
        if not isinstance(exc, json.JSONDecodeError):
            return f"not readable yet ({exc})"
        try:
            head = p.read_text(encoding="utf-8-sig", errors="replace").lstrip()[:1]
        except OSError:
            head = ""
        if head and head not in "{[":
            return ("not JSON – the flow writes plain text. In 'Create file' set File Content to "
                    "string(outputs('<your Compose step>')), not the 'Html to text' output")
        return f"incomplete JSON – still syncing? ({exc.msg})"

    async def fetch(self) -> list[InboxMessage]:
        msgs, errors = [], []
        if self.root is None or not self.root.is_dir():
            self._set_root(find_inbox_dir(self.configured))
            if self.root is None:
                self.files = 0
                return []
        paths = self._paths()
        self.files = len(paths)
        for p in paths:
            key = str(p)
            try:
                mtime = p.stat().st_mtime
                if self._done.get(key) == mtime:
                    continue
                with p.open("r", encoding="utf-8-sig") as f:  # read-only
                    data = json.load(f)
            except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                errors.append(f"{p.name}: {self._diagnose(p, exc)}")  # retried on the next poll
                continue
            self._done[key] = mtime
            if not isinstance(data, dict):
                continue
            msg = parse_file(p, self.channel, data)
            if msg is None or msg.received_at < self.not_before:
                continue
            if self.ignore and self._ignored(msg.sender):
                msg.meta["ignored"] = "sender is in OPS_IGNORE_SENDERS"  # shown in the feed, not triaged
            msgs.append(msg)
        for k in [k for k in self._done if not Path(k).exists()]:  # cleaned up by the retention flow
            del self._done[k]
        self.errors = errors[-5:]
        msgs.sort(key=lambda m: m.received_at)
        return msgs

    def info(self) -> dict[str, Any]:
        if self.root is None:
            return {"root": None, "dirs": [], "files": 0, "errors": [],
                    "waiting": self.configured or "~/Library/CloudStorage/OneDrive*/AgentInbox"}
        return {"root": str(self.root), "dirs": [str(d) for d in self.dirs if d.is_dir()], "files": self.files,
                "errors": self.errors}

"""TeamService: team definitions (``teams.yaml``), the scheduler loop and team runs.

A team is a lead profile + the specialists it may plan with + repos + standing orders (``goal``). It runs as a
normal mission (same map, report, feedback rounds) tagged with ``Topic.team``:

* **scheduled** – ``schedule`` is a cron expression in local time. After downtime a missed run is caught up once
  if it is not older than ``TEAMS_CATCH_UP_HOURS``. A run is skipped while the previous one is still active.
* **on request** – teams without a schedule (or any team via ▶ Run now) take a prompt from the human.

Definitions live in the versioned YAML file (hot-reloaded); run history + pause flags live in the DB (kv).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from .cron import Cron, CronError

if TYPE_CHECKING:
    from ..runtime.runtime import Runtime, Topic

log = logging.getLogger(__name__)

KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
TICK_SECONDS = 20.0
ON_TIME_SECONDS = 300  # a due run started within 5 min counts as "on schedule", later as "catch-up"
RUNS_KEPT = 25


@dataclass
class TeamSpec:
    key: str
    title: str
    area: str = "Teams"
    icon: str = "👥"
    description: str = ""
    lead: str = "orchestrator"  # orchestrator profile
    members: list[str] = field(default_factory=list)  # worker profiles the lead may use; [] = all
    repos: list[dict[str, Any]] = field(default_factory=list)  # [{repo, write, ref}]
    goal: str = ""  # standing orders; placeholders {date} {weekday} {prompt}
    prompt_hint: str = ""  # placeholder for the "on request" text box
    schedule: str | None = None
    mode: str = "hierarchy"
    model: str | None = None
    region: str | None = None  # region name – created on first run, so runs line up next to each other
    enabled: bool = True

    @classmethod
    def from_data(cls, d: dict[str, Any]) -> "TeamSpec":
        key = str(d.get("key") or "").strip().lower()
        if not KEY_RE.match(key):
            raise ValueError(f"invalid team key '{key}' (lowercase letters, digits, hyphens)")
        schedule = (str(d.get("schedule") or "").strip() or None)
        if schedule:
            Cron.parse(schedule)  # validate early
        repos = []
        for r in d.get("repos") or []:
            r = {"repo": r} if isinstance(r, str) else dict(r)
            if r.get("repo"):
                repos.append({"repo": str(r["repo"]), "write": bool(r.get("write")),
                              **({"ref": str(r["ref"])} if r.get("ref") else {})})
        mode = str(d.get("mode") or "hierarchy")
        return cls(key=key, title=str(d.get("title") or key)[:60], area=str(d.get("area") or "Teams")[:60],
                   icon=str(d.get("icon") or "👥")[:4], description=" ".join(str(d.get("description") or "").split()),
                   lead=str(d.get("lead") or "orchestrator"), members=[str(m) for m in d.get("members") or []],
                   repos=repos, goal=str(d.get("goal") or "").strip(), prompt_hint=str(d.get("prompt_hint") or ""),
                   schedule=schedule, mode=mode if mode in ("auto", "hierarchy", "swarm") else "hierarchy",
                   model=(str(d["model"]) if d.get("model") else None),
                   region=(str(d["region"]) if d.get("region") else None), enabled=bool(d.get("enabled", True)))

    @property
    def cron(self) -> Cron | None:
        return Cron.parse(self.schedule) if self.schedule else None

    @property
    def on_request(self) -> bool:
        """Needs a prompt from the human (no standing orders or they reference {prompt})."""
        return not self.goal or "{prompt}" in self.goal


def _fill(template: str, **values: str) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1), m.group(0)), template)


class TeamService:
    def __init__(self, runtime: "Runtime") -> None:
        self.rt = runtime
        s = runtime.settings
        self.path = Path(s.teams_file).expanduser().resolve() if s.teams_file else None
        self.scheduler_on = bool(s.teams_scheduler)
        self.catch_up = float(s.teams_catch_up_hours) * 3600
        self.teams: dict[str, TeamSpec] = {}
        self.errors: list[str] = []
        self.state: dict[str, dict[str, Any]] = {}  # key → {last_due, paused, runs: [...]}
        self._mtime: float | None = None
        self._task: asyncio.Task | None = None
        self.reload()

    # ------------------------------------------------------------ loading
    def reload(self, force: bool = False) -> None:
        if self.path is None:
            return
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            self.teams, self.errors, self._mtime = {}, [], None
            return
        if not force and mtime == self._mtime:
            return
        self._mtime = mtime
        errors: list[str] = []
        teams: dict[str, TeamSpec] = {}
        try:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
            for d in data.get("teams") or []:
                try:
                    t = TeamSpec.from_data(d if isinstance(d, dict) else {})
                    teams[t.key] = t
                except (ValueError, CronError) as exc:
                    errors.append(f"{(d or {}).get('key', '?')}: {exc}")
        except Exception as exc:  # noqa: BLE001 – a broken file must not stop the landscape
            errors.append(f"{self.path.name}: {exc}")
        self.teams, self.errors = teams, errors
        for e in errors:
            log.warning("Team definition problem – %s", e)

    def get(self, key: str) -> TeamSpec | None:
        self.reload()
        return self.teams.get(key)

    def team_state(self, key: str) -> dict[str, Any]:
        return self.state.setdefault(key, {"last_due": None, "paused": False, "runs": []})

    # ------------------------------------------------------------ persistence
    def dump(self) -> dict[str, Any]:
        return self.state

    def restore(self, data: dict[str, Any] | None) -> None:
        self.state = {k: {"last_due": v.get("last_due"), "paused": bool(v.get("paused")), "runs": v.get("runs") or []}
                      for k, v in (data or {}).items() if isinstance(v, dict)}

    def _changed(self) -> None:
        self.rt.persist.kv()
        self.rt.emit("teams_changed")

    # ------------------------------------------------------------ runs
    def active_run(self, key: str) -> "Topic | None":
        for r in reversed(self.team_state(key)["runs"]):
            t = self.rt.topics.get(r.get("topic_id") or "")
            if t is not None and t.status == "active":
                return t
        return None

    async def run(self, key: str, *, prompt: str | None = None, trigger: str = "manual") -> "Topic":
        team = self.get(key)
        if team is None:
            raise KeyError(key)
        prompt = (prompt or "").strip()
        if team.on_request and not prompt:
            raise ValueError(f"{team.title} works on request – describe what you want")
        if trigger != "manual" and self.active_run(key) is not None:
            raise RuntimeError("previous run is still active")
        from ..context.intel import ContextItem

        now = datetime.now()
        values = {"date": now.strftime("%Y-%m-%d"), "weekday": now.strftime("%A"), "prompt": prompt or ""}
        goal = _fill(team.goal, **values) if team.goal else prompt or ""
        if prompt and "{prompt}" not in team.goal and team.goal:
            goal += f"\n\n## Additional instructions from the human\n{prompt}"
        title = (f"{team.icon} {prompt[:44]}" if team.on_request and prompt
                 else f"{team.icon} {team.title} · {now.strftime('%a %d %b')}")
        new_region = ({"name": team.region, "description": team.description or team.area}
                      if team.region else None)
        topic = await self.rt.start_mission(
            goal, title=title, mode=team.mode, model=team.model, profile=team.lead, new_region=new_region,
            context=[ContextItem(kind="repo", **r) for r in team.repos], team=team.key)
        st = self.team_state(key)
        st["runs"] = [*st["runs"], {"topic_id": topic.id, "at": time.time(), "trigger": trigger,
                                    "prompt": (prompt or "")[:300] or None}][-RUNS_KEPT:]
        icon = {"schedule": "⏰", "catch-up": "⏰", "manual": "▶️"}.get(trigger, "▶️")
        self.rt.log(f"{icon} Team {team.icon} {team.title} deployed ({trigger})", topic_id=topic.id, level="success")
        self._changed()
        return topic

    def set_paused(self, key: str, paused: bool) -> None:
        if self.get(key) is None:
            raise KeyError(key)
        st = self.team_state(key)
        st["paused"] = paused
        if not paused:  # resuming never fires the runs missed while paused
            st["last_due"] = time.time()
        self._changed()

    # ------------------------------------------------------------ scheduler
    def next_run(self, key: str) -> float | None:
        team = self.teams.get(key)
        st = self.team_state(key)
        if team is None or not team.cron or not team.enabled or st["paused"]:
            return None
        base = datetime.fromtimestamp(st["last_due"] or time.time())
        return team.cron.next_after(base).timestamp()

    async def tick(self, now: float | None = None) -> list[str]:
        """Start every due team once. Returns the keys that were started (for tests / logs)."""
        self.reload()
        now = time.time() if now is None else now
        started: list[str] = []
        for key, team in list(self.teams.items()):
            cron = team.cron
            st = self.team_state(key)
            if cron is None or not team.enabled or st["paused"]:
                continue
            if st["last_due"] is None:  # first sight of this schedule: start counting from now
                st["last_due"] = now
                self._changed()
                continue
            due = cron.next_after(datetime.fromtimestamp(st["last_due"])).timestamp()
            if due > now:
                continue
            latest = due  # skip over all due times ≤ now – one run, not a burst
            while (nxt := cron.next_after(datetime.fromtimestamp(latest)).timestamp()) <= now:
                latest = nxt
            st["last_due"] = latest
            late = now - latest
            trigger = "schedule" if late <= ON_TIME_SECONDS else "catch-up"
            if late > max(self.catch_up, ON_TIME_SECONDS):
                self.rt.log(f"⏰ {team.title}: missed run at {datetime.fromtimestamp(latest):%a %H:%M} "
                            "is too old – skipped", level="warn")
                self._changed()
                continue
            try:
                await self.run(key, trigger=trigger)
                started.append(key)
            except Exception as exc:  # noqa: BLE001
                self.rt.log(f"⏰ {team.title}: scheduled run skipped – {exc}", level="warn")
                self._changed()
        return started

    async def _loop(self) -> None:
        while True:
            with contextlib.suppress(Exception):
                await self.tick()
            await asyncio.sleep(TICK_SECONDS)

    def start(self) -> None:
        if self.scheduler_on and self.path is not None and self._task is None:
            self._task = asyncio.create_task(self._loop(), name="team-scheduler")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    # ------------------------------------------------------------ view
    def _profile_view(self, key: str) -> dict[str, Any]:
        p = self.rt.profiles.get(key)
        return {"key": key, "icon": p.icon if p else "❔", "title": p.title if p else key, "missing": p is None,
                "checks": bool(p and "checks" in p.tools), "write": bool(p and "repo_write" in p.tools)}

    def snapshot(self) -> dict[str, Any]:
        self.reload()
        teams = []
        for key, t in self.teams.items():
            st = self.team_state(key)
            runs = []
            for r in reversed(st["runs"][-8:]):
                topic = self.rt.topics.get(r.get("topic_id") or "")
                runs.append({**r, "title": topic.title if topic else "(dismissed)",
                             "status": topic.status if topic else "gone", "rounds": topic.rounds if topic else 0})
            cron = t.cron
            teams.append({**asdict(t), "schedule_label": cron.describe() if cron else None,
                          "on_request": t.on_request, "paused": st["paused"], "next_run": self.next_run(key),
                          "active_topic": (a.id if (a := self.active_run(key)) else None),
                          "lead_profile": self._profile_view(t.lead),
                          "member_profiles": [self._profile_view(m) for m in t.members], "runs": runs})
        return {"enabled": self.path is not None, "file": str(self.path) if self.path else None,
                "scheduler": self._task is not None, "catch_up_hours": self.catch_up / 3600,
                "errors": self.errors, "teams": teams}

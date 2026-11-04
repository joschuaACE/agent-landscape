"""Mission reports, built from the A2A tasks of a mission's context (contextId == topic id).

Every delegation is an A2A task carrying the order (first user message) and the result (artifacts), so a
report is a faithful record of who received which input and produced which output, per feedback round.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from ..a2a.types import Task, parts_text

if TYPE_CHECKING:
    from .runtime import Runtime, Topic


def _md(task: Task) -> dict[str, Any]:
    return task.metadata or {}


def _input(task: Task) -> str:
    for m in task.history:
        if m.role == "user":
            return m.text
    return ""


def _output(task: Task) -> str:
    text = "\n\n".join(parts_text(a.parts) for a in task.artifacts).strip()
    if not text and task.status.message:
        text = task.status.message.text
    return text


def _entry(task: Task, rt: "Runtime") -> dict[str, Any]:
    md = _md(task)
    agent = rt.agents.get(md.get("agent_id", ""))
    started, finished = md.get("created_at"), md.get("finished_at")
    return {
        "task_id": task.id,
        "agent_id": md.get("agent_id"),
        "agent_name": md.get("agent_name") or (agent.name if agent else md.get("agent_id")),
        "icon": md.get("icon") or (agent.icon if agent else "🤖"),
        "role": md.get("role") or (agent.role if agent else ""),
        "title": md.get("title") or (agent.title if agent else ""),
        "level": agent.level if agent else None,
        "parent_id": md.get("parent_id"),
        "subtask": md.get("subtask"),
        "state": task.status.state.value,
        "input": _input(task),
        "output": _output(task),
        "started_at": started,
        "finished_at": finished,
        "duration": round(finished - started, 1) if started and finished else None,
        "tokens": md.get("tokens", 0),
        "cost": md.get("cost", 0.0),
        "model": md.get("model"),
        "profile": md.get("profile"),
        "profile_rev": md.get("profile_rev"),
        "profile_source": md.get("profile_source"),
        "skills_used": md.get("skills_used", []),
        "tool_calls": md.get("tool_calls", []),
    }


def _tree_order(entries: list[dict[str, Any]], root_ids: set[str]) -> list[dict[str, Any]]:
    """Depth-first order (parent before children) with a ``depth`` per entry."""
    by_parent: dict[str | None, list[dict[str, Any]]] = {}
    for e in entries:
        by_parent.setdefault(e["parent_id"], []).append(e)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def walk(parent: str | None, depth: int) -> None:
        for e in sorted(by_parent.get(parent, []), key=lambda x: x["started_at"] or 0):
            if e["task_id"] in seen:
                continue
            seen.add(e["task_id"])
            out.append({**e, "depth": depth})
            # an agent can serve several tasks in one round – only descend once per agent
            walk(e["agent_id"], depth + 1)

    for rid in root_ids:
        walk(rid, 0)
    out += [{**e, "depth": 0} for e in entries if e["task_id"] not in seen]  # orphans (e.g. evicted parents)
    return out


def build_report(rt: "Runtime", topic: "Topic") -> dict[str, Any]:
    tasks = sorted(rt.tasks.list(context_id=topic.id), key=lambda t: _md(t).get("created_at") or 0)
    commander_id = rt.commander.id if rt.commander else "commander"
    cmd_tasks = [t for t in tasks if _md(t).get("agent_id") == commander_id]
    starts = [_md(t).get("created_at") or 0 for t in cmd_tasks] or [0]

    rounds: list[dict[str, Any]] = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else float("inf")
        cmd = cmd_tasks[i] if i < len(cmd_tasks) else None
        members = [t for t in tasks if t is not cmd and t not in cmd_tasks
                   and start <= (_md(t).get("created_at") or 0) < end]
        entries = [_entry(t, rt) for t in members]
        artifacts = _tree_order(entries, {commander_id})
        head = _entry(cmd, rt) if cmd else None
        rounds.append({
            "round": i + 1,
            "kind": "mission" if i == 0 else "feedback",
            "input": head["input"] if head else topic.goal,
            "deliverable": head["output"] if head else (topic.result or ""),
            "state": head["state"] if head else topic.status,
            "started_at": head["started_at"] if head else None,
            "finished_at": head["finished_at"] if head else None,
            "duration": head["duration"] if head else None,
            "tokens": sum(e["tokens"] or 0 for e in entries) + (head["tokens"] if head else 0),
            "cost": round(sum(e["cost"] or 0 for e in entries) + (head["cost"] if head else 0), 6),
            "artifacts": artifacts,
        })

    agent_ids = list(dict.fromkeys(e["agent_id"] for r in rounds for e in r["artifacts"] if e["agent_id"]))
    team = [rt.agents[a].to_dict() for a in agent_ids if a in rt.agents]
    region = rt.regions.get(topic.region_id or "")
    return {
        "topic": topic.to_dict(),
        "region": region.to_dict() if region else None,
        "rounds": rounds,
        "team": [{k: m[k] for k in ("id", "name", "icon", "role", "title", "level", "veteran", "tokens", "cost",
                                    "tasks_done", "status", "parent_id")} for m in team],
        "totals": {
            "rounds": len(rounds),
            "a2a_tasks": sum(len(r["artifacts"]) for r in rounds) + len(cmd_tasks),
            "units": len(agent_ids),
            "tokens": topic.tokens,
            "cost": round(topic.cost, 6),
            "duration": round((topic.finished_at or 0) - topic.created_at, 1) if topic.finished_at else None,
        },
    }


# ------------------------------------------------------------------ markdown
def _quote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in (text or "").strip().splitlines())


def _demote(text: str, levels: int = 2) -> str:
    """Push headings of embedded deliverables down so they nest under the report's structure."""
    return re.sub(r"^(#{1,6})(\s)", lambda m: "#" * min(6, len(m.group(1)) + levels) + m.group(2),
                  text or "", flags=re.M)


def report_markdown(report: dict[str, Any]) -> str:
    t, totals, region = report["topic"], report["totals"], report["region"]
    lines = [
        f"# Mission Report: {t['title']}",
        "",
        f"- **Status:** {t['status']}  ",
        f"- **Region:** {region['icon']} {region['name']} ({region['biome_label']})  " if region else "- **Region:** –  ",
        f"- **Formation:** {t['mode']} · **Rounds:** {totals['rounds']} · **Units:** {totals['units']} · "
        f"**A2A tasks:** {totals['a2a_tasks']}  ",
        f"- **Tokens:** {totals['tokens']:,} · **Cost:** ${totals['cost']:.4f}"
        + (f" · **Duration:** {totals['duration']:.0f}s" if totals["duration"] else ""),
        "",
        "## Objective",
        "",
        t["goal"],
        "",
    ]
    if t.get("context"):
        lines += ["## Intel (attached context)", ""]
        lines += [f"- {'✅' if c.get('ok') else '❌'} **{c['kind']}** `{c.get('name')}`"
                  + (f" – {c['detail']}" if c.get("detail") else "") + (f" – {c['error']}" if c.get("error") else "")
                  for c in t["context"]]
        lines.append("")
    final = next((r for r in reversed(report["rounds"]) if r["deliverable"]), None)
    if final:
        lines += [f"## Final deliverable (round {final['round']})", "", _demote(final["deliverable"]), ""]
    for r in report["rounds"]:
        lines += [f"## Round {r['round']} – {'Mission' if r['kind'] == 'mission' else 'Feedback'} ({r['state']})", ""]
        if r["kind"] == "feedback":
            lines += ["**Feedback:**", "", _quote(r["input"]), ""]
            if r["deliverable"] and r is not final:
                lines += ["**Deliverable of this round:**", "", _demote(r["deliverable"], 3), ""]
        for a in r["artifacts"]:
            indent = "  " * a["depth"]
            lines += [f"### {indent}{a['icon']} {a['agent_name']} – {a['subtask'] or a['title']}",
                      "", f"*{a['role']} · {a['state']} · {a['tokens']} tokens"
                      + (f" · {a['duration']}s" if a["duration"] else "") + "*", "",
                      "**Input**", "", _quote(a["input"]), ""]
            if a["tool_calls"]:
                lines += ["**Tool calls**", ""] + [f"- {'🔧' if c['ok'] else '⚠️'} `{c['label']}` ({c['ms']} ms)"
                                                   for c in a["tool_calls"]] + [""]
            lines += ["**Output**", "", _demote(a["output"], 3), ""]
    return "\n".join(lines).rstrip() + "\n"

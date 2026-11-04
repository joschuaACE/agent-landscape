"""Canned responses for mock mode (fed to LiteLLM's ``mock_response``)."""

from __future__ import annotations

import json
import random

_SUBTASKS = [
    ("Reconnaissance", "researcher", "Survey existing solutions, constraints and prior art."),
    ("Architecture", "engineer", "Design the technical architecture and main components."),
    ("Risk assessment", "critic", "Identify risks, failure modes and mitigations."),
    ("Strategy options", "analyst", "Compare strategic options and recommend one."),
    ("User journey", "designer", "Outline the key user flows and experience."),
    ("Briefing document", "writer", "Write the executive briefing for stakeholders."),
    ("Prototype plan", "engineer", "Define an MVP prototype and milestones."),
    ("Market scan", "researcher", "Scan competitors and market signals."),
]


def mock_plan(max_subtasks: int, *, allow_swarm: bool, allow_delegate: bool) -> str:
    n = random.randint(2, max(2, max_subtasks))
    picks = random.sample(_SUBTASKS, n)
    subtasks = []
    for i, (title, spec, instr) in enumerate(picks):
        strategy = "single"
        if allow_swarm and (i == 0 or random.random() < 0.25):
            strategy = "swarm"
        elif allow_delegate and i == 1 and random.random() < 0.7:
            strategy = "delegate"
        subtasks.append({"title": title, "instructions": instr, "specialty": spec, "strategy": strategy})
    random.shuffle(subtasks)
    return json.dumps({"subtasks": subtasks})


def mock_plan_from_team(team: list[tuple[str, str, str]]) -> str:
    """Feedback round: re-task the existing team. ``team`` = [(title, specialty, strategy)]."""
    return json.dumps({"subtasks": [
        {"title": f"Revise: {title}", "instructions": "Address the feedback in your area.",
         "specialty": spec, "strategy": strategy} for title, spec, strategy in team]})


def mock_report(who: str, task: str) -> str:
    first = task.strip().splitlines()[0][:120] if task.strip() else "the task"
    bullets = random.sample([
        "Key constraint identified and documented.",
        "Two viable approaches compared; the simpler one is preferred.",
        "Main risk is integration complexity – mitigated by incremental delivery.",
        "Estimated effort: 2–3 iterations.",
        "Dependencies mapped and owners assigned.",
        "Quick win available in the first iteration.",
        "Open question flagged for the orchestrator.",
    ], 3)
    return f"### {who} report\n**Task:** {first}\n\n" + "\n".join(f"- {b}" for b in bullets)


def mock_synthesis(who: str, n_reports: int) -> str:
    return (f"## Final deliverable ({who})\n\nMerged {n_reports} reports into a coherent plan:\n"
            "1. Start with the reconnaissance findings.\n2. Apply the recommended architecture.\n"
            "3. Mitigate the top risks early.\n4. Ship an MVP, then iterate.\n")

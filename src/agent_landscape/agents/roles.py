"""Callsigns + prompt builders. Agent prompts themselves live in profiles (agents/profiles/*.md,
overridable in the repo's profiles/ folder) – see agents/profiles.py."""

from __future__ import annotations

from typing import Any

CALLSIGNS = [
    "Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf", "Hotel", "India", "Juliet",
    "Kilo", "Lima", "Mike", "November", "Oscar", "Papa", "Quebec", "Romeo", "Sierra", "Tango",
    "Uniform", "Victor", "Whiskey", "X-ray", "Yankee", "Zulu",
]


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:  # unknown {placeholders} in user prompts stay as they are
        return "{" + key + "}"


def fill(template: str, **values: Any) -> str:
    try:
        return template.format_map(_SafeDict(values))
    except (ValueError, IndexError):  # stray braces in a user prompt → use it verbatim
        return template


def models_text(models: list[str]) -> str:
    """Auto model mode: the pool the orchestrator may choose from, per subtask."""
    lines = [f'- "{m}"' + (" (no tools – text-only work such as writing or synthesis)"
                             if m.startswith("claude-code/") else "") for m in models]
    return ("Models (auto mode): set \"model\" per subtask to one of these, matched to its complexity – a "
            "smaller/faster model for simple lookups, summaries and formatting; a stronger model for complex "
            "reasoning, architecture, code changes and reviews. Subtasks that need tools (files, repos, knowledge, "
            "checks) must not use a 'no tools' model.\n" + "\n".join(lines))


def plan_system_prompt(profile: Any, workers: list[Any], max_subtasks: int, allow_delegate: bool,
                       allow_swarm: bool, allies: list[dict], models: list[str] | None = None) -> str:
    """Orchestrator planning prompt: profile persona + plan template, plus the parts the code relies on
    (available specialists, strategies, JSON format) which users cannot break by editing."""
    strategies = ['- "single": one specialist agent handles the subtask.']
    if allow_swarm:
        strategies.append('- "swarm": several agents with different perspectives work in parallel and a '
                          'swarm leader merges their answers (open-ended, creative or uncertain work).')
    if allow_delegate:
        strategies.append('- "delegate": a sub-orchestrator with its own team handles it (only for large, '
                          'multi-part subtasks).')
    if allies:
        strategies.append('- "ally": hand the subtask to an external allied A2A agent (set "ally_id").')
    ally_text = ""
    if allies:
        ally_text = "\nAllied agents:\n" + "\n".join(
            f'- id="{a["id"]}" {a["name"]}: {a["description"]}' for a in allies)
    specialists = "\n".join(f'- "{w.key}" ({w.title}){" ✍️ can edit repos" if "repo_write" in w.tools else ""}'
                            f'{" 🧪 can run tests/checks" if "checks" in w.tools else ""}: '
                            f'{w.description or w.prompt[:120]}' for w in workers)
    plan = profile.prompts.get("plan") or (
        "Break the mission into at most {max_subtasks} focused subtasks that specialist agents can execute in parallel.")
    first = workers[0].key if workers else "researcher"
    model_part = f"\n\n{models_text(models)}" if models else ""
    model_field = f', "model": "{models[0]}"' if models else ""
    return (
        f"{profile.prompt}\n\n{fill(plan, max_subtasks=max_subtasks)}\n\n"
        f"Specialists (use the key in quotes as \"specialty\"):\n{specialists}\n\n"
        "Strategies:\n" + "\n".join(strategies) + ally_text + model_part + "\n\n"
        f"Use at most {max_subtasks} subtasks. "
        'Respond ONLY with JSON: {"subtasks": [{"title": "...", "instructions": "...", '
        f'"specialty": "{first}", "strategy": "single"{model_field}}}]}}'
    )


def synth_system_prompt(profile: Any, default: str) -> str:
    """Synthesis prompt (orchestrator / hive leader): persona + synthesize template."""
    return f"{profile.prompt}\n\n{profile.prompts.get('synthesize') or default}".strip()

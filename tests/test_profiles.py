"""Agent profiles (prompts / model / tool access) and repo skills."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_landscape.agents.profiles import Profile, ProfileRegistry
from agent_landscape.agents.roles import plan_system_prompt
from agent_landscape.config import Settings
from agent_landscape.context.intel import Doc, MissionIntel
from agent_landscape.context.mcp import MCPGateway
from agent_landscape.context.skills import SkillRegistry
from agent_landscape.context.toolbox import Toolbox


def write(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_builtins_cover_todays_roles():
    reg = ProfileRegistry(None)
    assert not reg.errors
    assert {p.key for p in reg.workers()} == {"researcher", "engineer", "writer", "analyst", "critic", "designer"}
    assert len(reg.by_kind("swarm_member")) == 6
    assert reg.get("orchestrator").prompts["plan"] and "knowledge_admin" in reg.get("orchestrator").tools
    assert "knowledge_admin" not in reg.get("researcher").tools


def test_repo_override_new_profile_and_reset(tmp_path):
    reg = ProfileRegistry(tmp_path)
    write(tmp_path / "researcher.md", "---\nkind: worker\ntitle: Deep Scout\nmodel: my-model\ntools: [intel]\n---\nBe thorough.")
    write(tmp_path / "copilot-expert.md", "---\nkind: worker\ntitle: Copilot Expert\nicon: 🤖\n"
                                          "description: Copilot Studio topics\nskills: [copilot-topics]\n---\nYou know Copilot Studio.")
    write(tmp_path / "broken.md", "---\nkind: wizard\n---\nx")
    reg.reload()
    r = reg.get("researcher")
    assert r.source == "override" and r.title == "Deep Scout" and r.model == "my-model" and r.tools == ["intel"]
    assert reg.get("copilot-expert").source == "repo" and not reg.get("copilot-expert").allows_skill("other")
    assert any("invalid kind" in e["error"] for e in reg.errors)
    assert reg.remove("researcher") == "reset" and reg.get("researcher").source == "builtin"
    assert reg.remove("copilot-expert") == "deleted" and reg.get("copilot-expert") is None
    with pytest.raises(ValueError):
        reg.remove("engineer")  # built-ins can't be deleted


def test_save_roundtrip_and_validation(tmp_path):
    reg = ProfileRegistry(tmp_path)
    data = {**reg.get("orchestrator").to_dict(), "prompt": "You lead {team}.\nLine two.", "model": "opus"}
    prof = reg.save(data)
    text = (tmp_path / "orchestrator.md").read_text()
    assert text.startswith("---\nkey: orchestrator") and "plan: |" in text  # readable multi-line YAML
    assert prof.source == "override" and reg.get("orchestrator").model == "opus"
    with pytest.raises(ValueError, match="kind cannot change"):
        reg.save({**data, "kind": "worker"})
    with pytest.raises(ValueError, match="unknown tool families"):
        reg.save({**data, "tools": ["root_shell"]})
    with pytest.raises(PermissionError):
        ProfileRegistry(None).save(data)


def test_plan_prompt_lists_enabled_specialists_and_survives_braces(tmp_path):
    reg = ProfileRegistry(tmp_path)
    write(tmp_path / "designer.md", "---\nkind: worker\ntitle: Artisan\nenabled: false\n---\nx")
    write(tmp_path / "orchestrator.md", "---\nkind: orchestrator\ntitle: O\nprompts:\n  plan: 'Max {max_subtasks} tasks, JSON like {x}'\n---\nYou are O.")
    reg.reload()
    prompt = plan_system_prompt(reg.get("orchestrator"), reg.workers(), 3, True, True, [])
    assert prompt.startswith("You are O.") and "Max 3 tasks, JSON like {x}" in prompt
    assert '"researcher"' in prompt and '"designer"' not in prompt and 'Respond ONLY with JSON' in prompt


def test_skills_registry_and_progressive_disclosure(tmp_path):
    write(tmp_path / "skills" / "copilot-topics" / "SKILL.md",
          "---\nname: copilot-topics\ndescription: Build Copilot Studio topics in YAML\n---\n# Steps\n1. Use trigger phrases.")
    write(tmp_path / "skills" / "copilot-topics" / "reference.md", "Topic YAML reference")
    write(tmp_path / "skills" / "copilot-topics" / ".env", "SECRET=1")
    write(tmp_path / "skills" / "no-desc" / "SKILL.md", "---\nname: no-desc\n---\nbody")
    reg = SkillRegistry([tmp_path / "skills", tmp_path / "missing"])
    assert list(reg.skills) == ["copilot-topics"] and reg.errors
    s = reg.get("copilot-topics")
    assert s.files == ["reference.md"] and "trigger phrases" in s.instructions()
    with pytest.raises(FileNotFoundError):
        s.read("../no-desc/SKILL.md")
    with pytest.raises(FileNotFoundError):
        s.read(".env")


async def test_toolbox_enforces_profile_tool_access(tmp_path):
    write(tmp_path / "skills" / "copilot-topics" / "SKILL.md",
          "---\nname: copilot-topics\ndescription: Build topics\n---\nUse YAML.")
    write(tmp_path / "skills" / "other" / "SKILL.md", "---\nname: other\ndescription: Other\n---\nOther.")
    rt = SimpleNamespace(skills=SkillRegistry([tmp_path / "skills"]), topics={}, regions={})
    intel = MissionIntel(roots=[tmp_path])
    intel.add_doc(Doc(name="brief.md", text="hello", source="file"))
    tb = Toolbox(intel, MCPGateway(Settings(mock=True)), mock=True, runtime=rt, topic_id="t")

    def agent(**kw):
        p = Profile(key="x", kind="worker", title="X", prompt="p", **kw)
        return SimpleNamespace(name="Agent X", role="worker", profile=p, skills_used=[])

    only_intel = agent(tools=["intel"], skills=[])
    names = {s["function"]["name"] for s in tb.specs(only_intel)}
    assert names == {"read_intel"}
    out, ok, _ = await tb.call("list_dir", {}, only_intel)
    assert not ok and "not available" in out

    skilled = agent(tools=["skills"], skills=["copilot-topics"])
    assert {s["function"]["name"] for s in tb.specs(skilled)} == {"use_skill", "read_skill_file"}
    assert "**copilot-topics**" in tb.briefing(skilled) and "**other**" not in tb.briefing(skilled)
    out, ok, _ = await tb.call("use_skill", {"name": "copilot-topics"}, skilled)
    assert ok and "Use YAML." in out and skilled.skills_used == ["copilot-topics"]
    out, ok, _ = await tb.call("use_skill", {"name": "other"}, skilled)
    assert not ok and "not available" in out

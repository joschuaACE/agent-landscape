"""Knowledge bases (vector stores): naming, scoping/permissions, seeding, ingest, archive."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_landscape.config import Settings
from agent_landscape.context.intel import Doc, MissionIntel
from agent_landscape.context.mcp import MCPGateway
from agent_landscape.context.toolbox import Toolbox
from agent_landscape.context.vectorstore import MemoryBackend, VectorService, slug


class FakeRuntime:
    def __init__(self, region_name="Copilot Studio"):
        self.region = SimpleNamespace(id="r1", name=region_name, biome="desert", kind="domain")
        self.topic = SimpleNamespace(id="t1", title="Transcript agent", goal="Build it", region_id="r1",
                                     result=None, rounds=1, feedback=[])
        self.regions = {"r1": self.region}
        self.topics = {"t1": self.topic}
        self.events = []

    def knowledge_changed(self, topic_id, store, **kw):
        self.events.append((store.name, kw.get("how")))


ORCH = SimpleNamespace(name="Orchestrator Alpha", role="orchestrator", depth=1, objective="plan")
WORKER = SimpleNamespace(name="Scout Bravo", role="worker", depth=2, objective="research")


def make(tmp_path=None, docs=True):
    rt = FakeRuntime()
    v = VectorService(MemoryBackend(), prefix="al")
    intel = MissionIntel(roots=[tmp_path] if tmp_path else [])
    if docs:
        intel.add_doc(Doc(name="brief.md", text="Transcripts live in the SharePoint recordings folder.", source="file"))
    tb = Toolbox(intel, MCPGateway(Settings(mock=True)), mock=True, vectors=v, runtime=rt, topic_id="t1")
    return rt, v, tb


def names(specs):
    return {s["function"]["name"] for s in specs}


def test_slug_and_unique_descriptive_names():
    v = VectorService(MemoryBackend())
    region = SimpleNamespace(name="Copilot Studio")
    assert slug("Transcript API — Research!") == "transcript-api-research"
    assert v.make_name("Copilot Studio", "Transcript API research", set()) == "al-copilot-studio-transcript-api-research"
    taken = {"al-copilot-studio-transcript-api-research"}
    assert v.make_name("Copilot Studio", "Transcript API research", taken).endswith("-2")
    # purpose already contains the region → no duplication
    assert v.make_name("Copilot Studio", "Copilot Studio topics", set()) == "al-copilot-studio-topics"
    assert v.region_store_name(region) == "al-copilot-studio-knowledge"


async def test_role_scoped_tools_and_permissions():
    rt, v, tb = make()
    assert {"kb_catalog", "kb_link", "kb_create", "kb_add"} <= names(tb.specs(ORCH))
    assert not names(tb.specs(WORKER)) & {"kb_create", "kb_add", "kb_link", "kb_catalog", "kb_search"}
    out, ok, _ = await tb.call("kb_create", {"purpose": "x"}, WORKER)
    assert not ok and "not available to Scout Bravo" in out

    out, ok, _ = await tb.call("kb_create", {"purpose": "transcript research", "description": "Sources for the team",
                                             "seed_with_intel": True}, ORCH)
    assert ok and "al-copilot-studio-transcript-research" in out and "Seeded with 1" in out
    store = v.linked("t1")[0]
    assert store.owned and store.metadata["created_by"] == "Orchestrator Alpha"
    assert store.metadata["mission_title"] == "Transcript agent" and store.metadata["region_name"] == "Copilot Studio"
    assert rt.events[0] == ("al-copilot-studio-transcript-research", "created")
    # sub-agents can now search it (spec recomputed), but still not write
    assert "kb_search" in names(tb.specs(WORKER))
    out, ok, _ = await tb.call("kb_search", {"store": store.name, "query": "where are transcripts"}, WORKER)
    assert ok and "SharePoint recordings" in out
    out, ok, _ = await tb.call("kb_add", {"store": store.name, "title": "t", "content": "x" * 30}, WORKER)
    assert not ok
    # duplicates are refused, the existing store is reused
    out, ok, _ = await tb.call("kb_create", {"purpose": "transcript research", "description": "again"}, ORCH)
    assert ok and "already exists" in out and len(v.linked("t1")) == 1


async def test_external_stores_are_read_only_and_unlinked_stores_hidden():
    rt, v, tb = make(docs=False)
    ext = await v.backend.create("customer_contracts", {})  # created outside Agent Landscape
    await v.backend.add_documents(ext.id, [("c.md", "Contract renewal is due in March.")])
    out, ok, _ = await tb.call("kb_search", {"store": "customer_contracts", "query": "renewal"}, WORKER)
    assert not ok and "not linked" in out
    out, ok, _ = await tb.call("kb_catalog", {"query": "contracts"}, ORCH)
    assert ok and "external, read-only" in out
    out, ok, _ = await tb.call("kb_link", {"store": "customer_contracts"}, ORCH)
    assert ok
    out, ok, _ = await tb.call("kb_search", {"store": "customer_contracts", "query": "renewal"}, WORKER)
    assert ok and "March" in out
    out, ok, _ = await tb.call("kb_add", {"store": "customer_contracts", "title": "x", "content": "y" * 30}, ORCH)
    assert not ok and "read-only" in out


async def test_ingest_folder_respects_sandbox(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("# Guide\nDeploy with the release pipeline.")
    (tmp_path / "app.py").write_text("print('hello')")
    (tmp_path / ".env").write_text("SECRET=1")
    rt, v, tb = make(tmp_path, docs=False)
    await tb.call("kb_create", {"purpose": "repo docs", "description": "Project docs"}, ORCH)
    store = v.linked("t1")[0]
    out, ok, _ = await tb.call("kb_ingest", {"store": store.name, "path": "", "pattern": "*"}, ORCH)
    assert ok and "Indexed 2 file(s)" in out
    indexed = " ".join(t for _, t in v.backend.docs[store.id])
    assert "release pipeline" in indexed and "SECRET" not in indexed
    out, ok, _ = await tb.call("kb_ingest", {"store": store.name, "path": "../"}, ORCH)
    assert not ok


async def test_archive_into_region_knowledge():
    rt, v, _ = make()
    rt.topic.result = "## Plan\nUse Graph API for transcripts."
    store = await v.archive(rt.topic, rt.region)
    assert store.name == "al-copilot-studio-knowledge" and store.metadata["kind"] == "region-knowledge"
    again = await v.archive(rt.topic, rt.region)  # same store reused
    assert again.id == store.id and store.files == 2
    hits = await v.backend.search(store.id, "graph api transcripts", 3)
    assert hits and "Transcript agent" in hits[0]["text"]

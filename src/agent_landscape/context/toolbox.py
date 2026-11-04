"""Per-mission toolbox: OpenAI-format tool specs + dispatch.

Tool families
* intel      – read_intel, list_dir, read_file, search_files     (attached context, read-only sandbox)
* knowledge  – kb_search (all agents, linked stores only)
               kb_catalog, kb_link, kb_create, kb_add, kb_ingest (orchestrators only)
* mcp        – tools selected from the LiteLLM MCP gateway

Specs are role-aware and recomputed every turn (a store created mid-loop becomes searchable at once).
"""

from __future__ import annotations

import fnmatch
import json
import re
from typing import TYPE_CHECKING, Any

from .intel import MissionIntel
from .mcp import MCPGateway, MCPTool
from .sandbox import FileSandbox, SandboxError, is_text, walk
from .vectorstore import StoreInfo, VectorService, slug

if TYPE_CHECKING:
    from ..runtime.runtime import Runtime

MAX_RESULT_CHARS = 12_000
INGEST_MAX_FILES = 40
FAMILY_OF = {"read_intel": "intel", "list_dir": "files", "read_file": "files", "search_files": "files",
             "kb_search": "knowledge", "kb_catalog": "knowledge_admin", "kb_link": "knowledge_admin",
             "kb_create": "knowledge_admin", "kb_add": "knowledge_admin", "kb_ingest": "knowledge_admin",
             "use_skill": "skills", "read_skill_file": "skills"}
# agents without a profile (tests, legacy records): access by role
ROLE_FAMILIES = {"orchestrator": {"intel", "files", "knowledge", "knowledge_admin", "mcp", "skills"},
                 "swarm_leader": set()}
DEFAULT_FAMILIES = {"intel", "files", "knowledge", "mcp", "skills"}
INGEST_MAX_BYTES = 2_000_000


def _fn(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties, "required": required or []}}}


def _safe_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:64]


class Toolbox:
    def __init__(self, intel: MissionIntel, mcp: MCPGateway, *, mock: bool = False,
                 vectors: VectorService | None = None, runtime: "Runtime | None" = None,
                 topic_id: str | None = None) -> None:
        self.intel = intel
        self.mcp = mcp
        self.mock = mock
        self.vectors = vectors if vectors is not None and vectors.enabled else None
        self.runtime = runtime
        self.topic_id = topic_id
        self.fs = FileSandbox(intel.roots) if intel.roots else None
        self._mcp_by_fn: dict[str, MCPTool] = {}
        names = [t.name for t in intel.tools]
        for t in intel.tools:
            fn = _safe_name(t.name if names.count(t.name) == 1 else f"{t.server}_{t.name}")
            self._mcp_by_fn[fn] = t
        self._granted: dict[str, dict[str, MCPTool]] = {}  # profile key@rev → MCP tools granted by the profile

    # ------------------------------------------------------------ access
    @staticmethod
    def families(agent: Any) -> set[str]:
        """Tool families this agent may use – from its profile, else by role."""
        prof = getattr(agent, "profile", None)
        if prof is not None:
            return set(prof.tools)
        return set(ROLE_FAMILIES.get(getattr(agent, "role", ""), DEFAULT_FAMILIES))

    @classmethod
    def can_write_kb(cls, agent: Any) -> bool:
        return "knowledge_admin" in cls.families(agent)

    @staticmethod
    def _grant_key(agent: Any) -> str | None:
        prof = getattr(agent, "profile", None)
        return f"{prof.key}@{prof.rev}" if prof is not None and prof.grant_mcp else None

    async def prepare(self, agent: Any) -> None:
        """Resolve MCP tools granted by the agent's profile (cached per profile revision)."""
        key = self._grant_key(agent)
        if key is None or key in self._granted:
            return
        tools = await self.mcp.select(agent.profile.grant_mcp)
        self._granted[key] = {_safe_name(t.name if t.name not in self._mcp_by_fn else f"{t.server}_{t.name}"): t
                              for t in tools}

    def _mcp_for(self, agent: Any) -> dict[str, MCPTool]:
        tools = dict(self._mcp_by_fn) if "mcp" in self.families(agent) else {}
        key = self._grant_key(agent)
        if key is not None:
            tools.update(self._granted.get(key, {}))
        return tools

    def _skills_for(self, agent: Any) -> list[Any]:
        reg = getattr(self.runtime, "skills", None)
        if reg is None or "skills" not in self.families(agent):
            return []
        prof = getattr(agent, "profile", None)
        return reg.available(prof.skills if prof is not None else ["*"])

    def linked(self) -> list[StoreInfo]:
        return self.vectors.linked(self.topic_id) if self.vectors and self.topic_id else []

    def _topic(self) -> Any:
        return self.runtime.topics.get(self.topic_id or "") if self.runtime else None

    def _region(self) -> Any:
        t = self._topic()
        return self.runtime.regions.get(t.region_id or "") if self.runtime and t else None

    # -------------------------------------------------------------- specs
    def specs(self, agent: Any = None) -> list[dict[str, Any]]:
        specs: list[dict[str, Any]] = []
        fam = self.families(agent)
        big_docs = [d.name for d in self.intel.docs.values()]
        if big_docs and "intel" in fam:
            specs.append(_fn("read_intel", "Read an attached mission document (file, web page or note) by name. "
                             "Use offset/limit to page through long documents.",
                             {"name": {"type": "string", "enum": big_docs[:50]},
                              "offset": {"type": "integer", "description": "start character (default 0)"},
                              "limit": {"type": "integer", "description": "max characters (default 8000)"}},
                             ["name"]))
        if self.fs and "files" in fam:
            specs += [
                _fn("list_dir", "List files and subfolders of an attached folder (read-only).",
                    {"path": {"type": "string", "description": "relative path inside the attached folder; empty = root"}}),
                _fn("read_file", "Read a text file inside the attached folders, with line numbers (max 400 lines per call).",
                    {"path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}},
                    ["path"]),
                _fn("search_files", "Case-insensitive text search across the attached folders (file names and contents).",
                    {"query": {"type": "string"}, "path": {"type": "string", "description": "optional sub-folder"}},
                    ["query"]),
            ]
        if self.vectors:
            specs += self._kb_specs(agent)
        skills = self._skills_for(agent)
        if skills:
            specs += [
                _fn("use_skill", "Load the full instructions of a skill before doing a task it covers. "
                    "Available skills are listed in your briefing.",
                    {"name": {"type": "string", "enum": [s.name for s in skills][:60]}}, ["name"]),
                _fn("read_skill_file", "Read an extra file (reference, template, example) that belongs to a skill.",
                    {"name": {"type": "string", "enum": [s.name for s in skills][:60]},
                     "path": {"type": "string", "description": "file path relative to the skill folder"}},
                    ["name", "path"]),
            ]
        for fn, t in self._mcp_for(agent).items():
            schema = t.input_schema if isinstance(t.input_schema, dict) else {}
            schema = {"type": "object", "properties": schema.get("properties", {}), "required": schema.get("required", [])}
            desc = f"[{t.server} via LiteLLM MCP]{' ⚠️ changes data –' if t.write else ''} {t.description}"[:1000]
            specs.append({"type": "function", "function": {"name": fn, "description": desc, "parameters": schema}})
        return specs

    def _kb_specs(self, agent: Any) -> list[dict[str, Any]]:
        specs: list[dict[str, Any]] = []
        linked = self.linked()
        if linked and "knowledge" in self.families(agent):
            specs.append(_fn(
                "kb_search", "Semantic search in a knowledge base (vector store) linked to this mission. "
                "Use it to find facts, prior decisions and reference material before answering.",
                {"store": {"type": "string", "enum": [s.name for s in linked]},
                 "query": {"type": "string", "description": "natural-language question or keywords"},
                 "limit": {"type": "integer", "description": "max hits (default 5)"}}, ["store", "query"]))
        if not self.can_write_kb(agent):
            return specs
        writable = [s.name for s in linked if s.owned] or ["(create one first)"]
        specs += [
            _fn("kb_catalog", "List existing knowledge bases (vector stores) you could link to this mission.",
                {"query": {"type": "string", "description": "optional filter by name/purpose"}}),
            _fn("kb_link", "Link an existing knowledge base to this mission so your sub-agents can search it.",
                {"store": {"type": "string", "description": "name or id from kb_catalog"}}, ["store"]),
            _fn("kb_create", "Create a new knowledge base (vector store) for this mission's team. The name is generated "
                "as <prefix>-<region>-<purpose> so it is unique and self-describing. It is linked to the mission "
                "automatically; sub-agents can then search it.",
                {"purpose": {"type": "string", "description": "2-5 words, e.g. 'transcript api research'"},
                 "description": {"type": "string", "description": "one sentence: what goes in and who uses it"},
                 "seed_with_intel": {"type": "boolean", "description": "also store the documents attached to the mission"}},
                ["purpose", "description"]),
            _fn("kb_add", "Add a document (findings, decisions, reference text) to a knowledge base created for this mission.",
                {"store": {"type": "string", "enum": writable}, "title": {"type": "string"},
                 "content": {"type": "string", "description": "markdown text"}}, ["store", "title", "content"]),
        ]
        if self.fs:
            specs.append(_fn(
                "kb_ingest", f"Index text files from the attached folders into a knowledge base "
                f"(max {INGEST_MAX_FILES} files / {INGEST_MAX_BYTES // 1_000_000} MB per call).",
                {"store": {"type": "string", "enum": writable},
                 "path": {"type": "string", "description": "folder or file inside the attached folders"},
                 "pattern": {"type": "string", "description": "optional glob on file names, e.g. '*.md'"}},
                ["store", "path"]))
        return specs

    # ----------------------------------------------------------- briefing
    def briefing(self, agent: Any = None, with_tools: bool = True) -> str:
        parts = [self.intel.briefing()] if not self.intel.empty else []
        skills = self._skills_for(agent) if with_tools else []
        if skills:
            parts.append("## Skills\nReusable playbooks from the repo. When your task matches one, call `use_skill` "
                         "first and follow its instructions:\n"
                         + "\n".join(f"- **{s.name}**: {s.description}" for s in skills[:40]))
        if self.vectors:
            linked = self.linked()
            writer = with_tools and self.can_write_kb(agent)
            if linked or writer:
                lines = ["## Knowledge bases (vector stores)"]
                if linked:
                    lines.append("Linked to this mission" + (" – search them with `kb_search`:" if with_tools else ":"))
                    for s in linked:
                        md = s.metadata or {}
                        lines.append(f"- **{s.name}** – {md.get('description') or md.get('purpose') or 'no description'}"
                                     f" ({s.files} files{', read-only' if not s.owned else ''})")
                else:
                    lines.append("None linked yet.")
                if writer:
                    lines.append(
                        "As an orchestrator you manage knowledge for your team: `kb_catalog` → `kb_link` to reuse an "
                        "existing base, or `kb_create` a dedicated one (named automatically) and fill it with `kb_add` / "
                        "`kb_ingest`. Create one when sub-agents need shared reference material (attached documents, "
                        "folder contents, research findings) – not for trivial tasks. Sub-agents can only *search* "
                        "bases that are linked to this mission.")
                parts.append("\n".join(lines))
        return "\n\n".join(parts)

    def label(self, name: str, args: dict[str, Any]) -> str:
        """Short human description for activity bubbles / reports."""
        key = next((args[k] for k in ("query", "path", "name", "store", "purpose", "title", "url", "input")
                    if args.get(k)), "")
        return f"{name} {str(key)[:48]}".strip()

    async def call(self, name: str, raw_args: str | dict[str, Any] | None,
                   agent: Any = None) -> tuple[str, bool, dict[str, Any]]:
        """Execute a tool → (result text, ok, parsed args). Never raises."""
        try:
            args = raw_args if isinstance(raw_args, dict) else json.loads(raw_args or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be an object")
        except Exception as exc:  # noqa: BLE001
            return f"Invalid tool arguments: {exc}", False, {}
        family = FAMILY_OF.get(name)
        mcp_tools = self._mcp_for(agent)
        if (family is not None and family not in self.families(agent)) or (family is None and name not in mcp_tools):
            return (f"Tool error: '{name}' is not available to {getattr(agent, 'name', 'this agent')} "
                    f"(check its profile's tool access)"), False, args
        try:
            if name in ("use_skill", "read_skill_file"):
                result = self._skill(name, args, agent)
            elif name == "read_intel":
                doc = self.intel.docs.get(str(args.get("name")))
                if doc is None:
                    raise SandboxError(f"Unknown document. Available: {', '.join(self.intel.docs)}")
                off = max(0, int(args.get("offset") or 0))
                lim = max(200, min(int(args.get("limit") or 8000), MAX_RESULT_CHARS))
                chunk = doc.text[off:off + lim]
                more = f"\n… ({doc.size - off - lim:,} more chars – offset={off + lim})" if off + lim < doc.size else ""
                result = f"{doc.name} [{off}:{off + len(chunk)} of {doc.size}]\n{chunk}{more}"
            elif name in ("list_dir", "read_file", "search_files") and self.fs:
                if name == "list_dir":
                    result = self.fs.list_dir(args.get("path"))
                elif name == "read_file":
                    result = self.fs.read_file(str(args.get("path", "")), args.get("start_line"), args.get("end_line"))
                else:
                    result = self.fs.search(str(args.get("query", "")), args.get("path"))
            elif name.startswith("kb_") and self.vectors:
                result = await self._kb(name, args, agent)
            elif name in mcp_tools:
                tool = mcp_tools[name]
                if self.mock:  # mock mode never calls external (possibly paid / writing) tools
                    result = f"[mock] {tool.server}/{tool.name} would be called with {json.dumps(args)[:300]}"
                else:
                    result = await self.mcp.call(tool, args)
            else:
                raise SandboxError(f"Unknown tool {name}")
        except Exception as exc:  # noqa: BLE001
            return f"Tool error: {exc}", False, args
        if len(result) > MAX_RESULT_CHARS:
            result = result[:MAX_RESULT_CHARS] + f"\n… (truncated, {len(result):,} chars total)"
        return result, True, args

    # ------------------------------------------------------------ skills
    def _skill(self, name: str, args: dict[str, Any], agent: Any) -> str:
        wanted = str(args.get("name") or "").strip()
        skill = next((s for s in self._skills_for(agent) if s.name == wanted), None)
        if skill is None:
            raise ValueError(f"Skill '{wanted}' is not available. Available: "
                             + (", ".join(s.name for s in self._skills_for(agent)) or "none"))
        used = getattr(agent, "skills_used", None)
        if isinstance(used, list) and skill.name not in used:
            used.append(skill.name)
        if name == "read_skill_file":
            return skill.read(str(args.get("path") or ""))
        files = skill.files
        return (f"# Skill: {skill.name}\n\n{skill.instructions()}"
                + (f"\n\n---\nExtra files (read with read_skill_file): {', '.join(files)}" if files else ""))

    # ---------------------------------------------------------- knowledge
    async def _kb(self, name: str, args: dict[str, Any], agent: Any) -> str:
        v = self.vectors
        assert v is not None and v.backend is not None and self.topic_id is not None
        if name == "kb_search":
            store = v.resolve_linked(self.topic_id, str(args.get("store", "")))
            if store is None:
                raise PermissionError("Store is not linked to this mission. Linked: "
                                      + (", ".join(s.name for s in self.linked()) or "none"))
            query = str(args.get("query", "")).strip()
            if not query:
                raise ValueError("query is required")
            hits = await v.backend.search(store.id, query, max(1, min(int(args.get("limit") or 5), 10)))
            v.touch(store, agent)
            if not hits:
                return f"No results in '{store.name}' for '{query}'."
            return f"{len(hits)} hit(s) in '{store.name}' for '{query}':\n\n" + "\n\n".join(
                f"[{i}] score {h['score']} · {h['filename']}\n{h['text'][:1500]}" for i, h in enumerate(hits, 1))

        if not self.can_write_kb(agent):
            raise PermissionError(f"{name} is reserved for orchestrators – ask your orchestrator")
        if name == "kb_catalog":
            words = str(args.get("query") or "").lower().split()
            linked_ids = {s.id for s in self.linked()}
            rows = []
            for s in sorted(await v.stores(refresh=True), key=lambda x: (not x.owned, x.name)):
                md = s.metadata or {}
                desc = md.get("description") or md.get("purpose") or ""
                hay = f"{s.name} {desc} {md.get('region_name') or ''}".lower()
                if words and not all(w in hay for w in words):
                    continue
                rows.append(f"- {s.name} · {s.files} files · {'agent-landscape' if s.owned else 'external, read-only'}"
                            f"{' · LINKED' if s.id in linked_ids else ''}{' – ' + str(desc) if desc else ''}")
                if len(rows) >= 40:
                    break
            return ("Knowledge bases:\n" + "\n".join(rows)) if rows else "No matching knowledge bases."
        if name == "kb_link":
            store = await v.find(str(args.get("store", "")))
            if store is None:
                raise ValueError("No knowledge base with that name or id – use kb_catalog")
            self._link(store, agent, "linked")
            return f"Linked '{store.name}' ({store.files} files) – your sub-agents can now search it with kb_search."
        if name == "kb_create":
            purpose = " ".join(str(args.get("purpose") or "").split())[:80]
            if len(purpose) < 3:
                raise ValueError("purpose is required (2-5 words)")
            description = " ".join(str(args.get("description") or purpose).split())[:400]
            existing = [s for s in self.linked() if s.owned and slug(purpose, 30) in s.name]
            if existing:
                return f"'{existing[0].name}' already exists for this purpose and is linked – use it."
            store = await v.create(purpose, description, region=self._region(), topic=self._topic(), agent=agent)
            self._link(store, agent, "created")
            seeded = ""
            if args.get("seed_with_intel") and self.intel.docs:
                docs = [(f"{slug(d.name, 50)}.md", f"# {d.name}\n\nSource: {d.origin or d.source}\n\n{d.text}")
                        for d in self.intel.docs.values()]
                await v.add(store, docs)
                store.files += len(docs)
                self._changed(store)
                seeded = f" Seeded with {len(docs)} attached document(s)."
            return (f"Created knowledge base '{store.name}' and linked it to this mission.{seeded} "
                    f"Add material with kb_add / kb_ingest; sub-agents search it with kb_search.")

        store = v.resolve_linked(self.topic_id, str(args.get("store", "")))
        if store is None:
            raise ValueError("Store is not linked to this mission – create it with kb_create or link it first")
        if name == "kb_add":
            title = " ".join(str(args.get("title") or "note").split())[:120]
            content = str(args.get("content") or "").strip()
            if len(content) < 20:
                raise ValueError("content is too short")
            doc = (f"# {title}\n\n- Added by: {getattr(agent, 'name', '?')}\n"
                   f"- Mission: {getattr(self._topic(), 'title', '')}\n\n{content}")
            await v.add(store, [(f"{slug(title, 60)}.md", doc)])
            store.files += 1
            v.touch(store, agent)
            self._changed(store)
            return f"Added '{title}' to '{store.name}'."
        if name == "kb_ingest":
            if not self.fs:
                raise SandboxError("No folders are attached to this mission")
            base = self.fs.resolve(str(args.get("path") or ""))
            pattern = str(args.get("pattern") or "*")
            files = [base] if base.is_file() else [p for p in walk(base, limit=2000) if fnmatch.fnmatch(p.name, pattern)]
            docs, total, skipped = [], 0, 0
            for p in files:
                size = p.stat().st_size
                if size == 0 or size > 300_000 or not is_text(p) \
                        or len(docs) >= INGEST_MAX_FILES or total + size > INGEST_MAX_BYTES:
                    skipped += 1
                    continue
                rel = self.fs.rel(p)
                text = p.read_text(errors="replace")
                if p.suffix.lower() not in (".md", ".txt"):
                    text = f"# {rel}\n\n```{p.suffix.lstrip('.')}\n{text}\n```"
                docs.append((f"{slug(rel, 80)}.md", text))
                total += size
            if not docs:
                raise ValueError(f"No indexable text files under '{self.fs.rel(base) or '.'}' matching '{pattern}'")
            await v.add(store, docs)
            store.files += len(docs)
            v.touch(store, agent)
            self._changed(store)
            return (f"Indexed {len(docs)} file(s) ({total // 1024} KB) into '{store.name}'"
                    + (f"; skipped {skipped} (binary, too large or over the per-call limit)." if skipped else "."))
        raise ValueError(f"Unknown knowledge tool {name}")

    def _link(self, store: StoreInfo, agent: Any, how: str) -> None:
        assert self.vectors is not None and self.topic_id is not None
        self.vectors.link(self.topic_id, store)
        self.vectors.touch(store, agent)
        if self.runtime is not None:
            self.runtime.knowledge_changed(self.topic_id, store, by=getattr(agent, "name", None), how=how)

    def _changed(self, store: StoreInfo) -> None:
        if self.runtime is not None and self.topic_id:
            self.runtime.knowledge_changed(self.topic_id, store)

    # --------------------------------------------------------------- mock
    def mock_plan(self, agent: Any = None) -> list[tuple[str, dict[str, Any]]]:
        """Deterministic tool calls used in mock mode to exercise the plumbing."""
        plan: list[tuple[str, dict[str, Any]]] = []
        if self.fs:
            plan.append(("list_dir", {}))
        if self.intel.docs:
            plan.append(("read_intel", {"name": next(iter(self.intel.docs)), "limit": 1500}))
        if self.vectors:
            topic = self._topic()
            own = [s for s in self.linked() if s.owned and (s.metadata or {}).get("kind") != "region-knowledge"]
            if self.can_write_kb(agent) and getattr(agent, "depth", 0) == 1 and not own and topic is not None:
                plan.append(("kb_create", {"purpose": f"{topic.title} research",
                                           "description": f"Shared reference material for '{topic.title}'.",
                                           "seed_with_intel": True}))
            elif self.linked() and "knowledge" in self.families(agent):
                plan.append(("kb_search", {"store": self.linked()[0].name,
                                           "query": getattr(agent, "objective", "") or "key facts"}))
        skills = self._skills_for(agent)
        if skills and getattr(agent, "role", "") in ("worker", "swarm_member"):
            plan.append(("use_skill", {"name": skills[0].name}))
        if not plan and self._mcp_for(agent):
            plan.append((next(iter(self._mcp_for(agent))), {}))
        return [(n, a) for n, a in plan if FAMILY_OF.get(n) is None or FAMILY_OF[n] in self.families(agent)]

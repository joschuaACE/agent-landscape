"""Vector stores ("knowledge bases") for agents.

Thin client for the vector store REST API at ``VECTOR_STORE_URL`` plus the policy layer:

* naming   – ``al-<region>-<purpose>`` (unique, descriptive), provenance in store metadata
* scoping  – stores are *linked* to a mission; sub-agents can only search linked stores
* writing  – only stores created by Agent Landscape (metadata.owner) can be written to
* archive  – completed missions (full mission report) are archived into the region's knowledge store
             (``al-<region>-knowledge``) so later missions in the same area can build on them
* recall   – before routing a new mission, Command HQ searches *all* region knowledge stores for
             related past missions (``recall``) and briefs the orchestrator with the best hits

In mock mode an in-memory backend is used, so demos and tests never touch the real service.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

log = logging.getLogger(__name__)

OWNER = "agent-landscape"
MAX_NAME = 63
MAX_ARCHIVE_CHARS = 300_000  # cap for one archived mission report

_REPORT_TITLE = re.compile(r"^#\s*Mission(?: Report)?:\s*(.+?)\s*$", re.M)
_REPORT_REGION = re.compile(r"^-\s*\*\*Region:\*\*\s*(.+?)\s*$", re.M)


def parse_report(text: str) -> tuple[str | None, str | None]:
    """(title, region name) of an exported mission report (``# Mission Report: …`` / ``- **Region:** 🏜️ X (Desert)``)."""
    m = _REPORT_TITLE.search(text or "")
    title = m.group(1) if m else None
    m = _REPORT_REGION.search(text or "")
    if not m:
        return title, None
    raw = re.sub(r"\s*\([^)]*\)\s*$", "", m.group(1)).strip()  # drop "(Biome label)"
    parts = raw.split(None, 1)
    if parts and not re.match(r"\w", parts[0]):  # drop the leading region icon
        raw = parts[1] if len(parts) > 1 else ""
    raw = raw.strip()
    return title, (raw if raw and raw not in ("–", "-") else None)


def recall_briefing(hits: list[dict[str, Any]], snippet_chars: int = 600) -> str:
    """Markdown section for the orchestrator's order: related past missions found by ``VectorService.recall``."""
    if not hits:
        return ""
    lines = ["## Recall: related past missions",
             "Command HQ searched the mission archive (region knowledge bases) for similar earlier work. Build on it "
             "where it applies – don't redo solved parts, and verify anything that may be outdated. The source "
             "knowledge bases are linked to this mission – use `kb_search` on them for details.", ""]
    for h in hits:
        s: StoreInfo = h["store"]
        region = (s.metadata or {}).get("region_name")
        snippet = " ".join(str(h.get("text") or "").split())
        if len(snippet) > snippet_chars:
            snippet = snippet[:snippet_chars].rstrip() + " …"
        lines.append(f"- **{h.get('filename') or 'document'}** in `{s.name}`"
                     + (f" (region {region}" if region else " (") + f", score {h.get('score')}): {snippet}")
    return "\n".join(lines)


def slug(text: str, max_len: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:max_len].rstrip("-") or "general"


@dataclass
class StoreInfo:
    id: str
    name: str
    files: int = 0
    bytes: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: int = 0

    @property
    def owned(self) -> bool:
        return (self.metadata or {}).get("owner") == OWNER

    def brief(self) -> dict[str, Any]:
        md = self.metadata or {}
        return {"id": self.id, "name": self.name, "files": self.files, "bytes": self.bytes, "owned": self.owned,
                "purpose": md.get("purpose"), "region": md.get("region_name"), "created_by": md.get("created_by"),
                "mission": md.get("mission_title"), "kind": md.get("kind"), "created_at": self.created_at}


def _info(d: dict[str, Any]) -> StoreInfo:
    return StoreInfo(id=d["id"], name=d.get("name", ""), files=int((d.get("file_counts") or {}).get("completed", 0)
                                                                    or (d.get("file_counts") or {}).get("total", 0)),
                     bytes=int(d.get("usage_bytes") or 0), metadata=d.get("metadata") or {},
                     created_at=int(d.get("created_at") or 0))


class Backend(Protocol):
    async def list(self) -> list[StoreInfo]: ...
    async def create(self, name: str, metadata: dict[str, Any]) -> StoreInfo: ...
    async def search(self, store_id: str, query: str, limit: int) -> list[dict[str, Any]]: ...
    async def add_documents(self, store_id: str, docs: list[tuple[str, str]]) -> None: ...
    async def delete(self, store_id: str) -> None: ...
    async def close(self) -> None: ...


class HTTPBackend:
    """REST client for the vector store service (OpenAPI at ``{url}/openapi.json``)."""

    def __init__(self, url: str, key: str | None, parser: str = "markitdown") -> None:
        self.url = url.rstrip("/")
        self.parser = parser
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=5),
                                      headers={"Authorization": f"Bearer {key}"} if key else {})

    async def _check(self, r: httpx.Response) -> Any:
        if r.status_code >= 400:
            detail = r.text[:300]
            try:
                detail = str(r.json().get("detail", detail))[:300]
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(f"vector store {r.request.method} {r.request.url.path} → {r.status_code}: {detail}")
        return r.json() if r.content else {}

    async def list(self) -> list[StoreInfo]:
        data = await self._check(await self.http.get(f"{self.url}/vector/ids"))
        return [_info(d) for d in data.get("data", [])]

    async def create(self, name: str, metadata: dict[str, Any]) -> StoreInfo:
        return _info(await self._check(await self.http.post(f"{self.url}/vector/store",
                                                            json={"name": name, "metadata": metadata})))

    async def search(self, store_id: str, query: str, limit: int) -> list[dict[str, Any]]:
        data = await self._check(await self.http.post(f"{self.url}/vector/store/{store_id}/search",
                                                      json={"query": query, "limit": limit}))
        hits = []
        for h in data.get("data", []):
            text = "\n".join(c.get("text", "") for c in h.get("content") or [] if c.get("type") == "text")
            hits.append({"filename": h.get("filename"), "score": round(float(h.get("score") or 0), 3), "text": text})
        return hits

    async def add_documents(self, store_id: str, docs: list[tuple[str, str]]) -> None:
        # the service's /embedding endpoint is unreliable → upload as files (parsed + chunked server-side);
        # many files go in one multipart request
        files = [("file_objects", (fn, text.encode("utf-8"), "text/markdown")) for fn, text in docs]
        await self._check(await self.http.put(f"{self.url}/vector/store/{store_id}/file", files=files,
                                              data={"parser": self.parser}, timeout=300))

    async def delete(self, store_id: str) -> None:
        r = await self.http.delete(f"{self.url}/vector/store/{store_id}")
        if r.status_code >= 400 and any(s.id == store_id for s in await self.list()):  # service may 500 after deleting
            await self._check(r)

    async def close(self) -> None:
        await self.http.aclose()


class MemoryBackend:
    """In-memory stand-in (mock mode / tests): keyword-overlap scoring, same interface."""

    def __init__(self) -> None:
        self.stores: dict[str, StoreInfo] = {}
        self.docs: dict[str, list[tuple[str, str]]] = {}

    async def list(self) -> list[StoreInfo]:
        return list(self.stores.values())

    async def create(self, name: str, metadata: dict[str, Any]) -> StoreInfo:
        s = StoreInfo(id=uuid.uuid4().hex, name=name, metadata=dict(metadata), created_at=int(time.time()))
        self.stores[s.id], self.docs[s.id] = s, []
        return s

    async def search(self, store_id: str, query: str, limit: int) -> list[dict[str, Any]]:
        if store_id not in self.stores:
            raise RuntimeError("vector store not found")
        words = {w for w in re.findall(r"\w+", query.lower()) if len(w) > 2}
        scored = []
        for fn, text in self.docs[store_id]:
            tw = set(re.findall(r"\w+", text.lower()))
            score = len(words & tw) / (len(words) or 1)
            if score > 0:
                scored.append({"filename": fn, "score": round(score, 3), "text": text[:1200]})
        return sorted(scored, key=lambda h: -h["score"])[:limit]

    async def add_documents(self, store_id: str, docs: list[tuple[str, str]]) -> None:
        if store_id not in self.stores:
            raise RuntimeError("vector store not found")
        s = self.stores[store_id]
        for filename, text in docs:
            self.docs[store_id].append((filename, text))
            s.files += 1
            s.bytes += len(text.encode())

    async def delete(self, store_id: str) -> None:
        self.stores.pop(store_id, None)
        self.docs.pop(store_id, None)

    async def close(self) -> None:
        pass


class VectorService:
    def __init__(self, backend: Backend | None, *, prefix: str = "al", auto_archive: bool = True,
                 kind: str = "http") -> None:
        self.backend = backend
        self.prefix = slug(prefix, 8)
        self.auto_archive = auto_archive
        self.kind = kind  # http | memory | off
        self._cache: tuple[float, list[StoreInfo]] | None = None
        self._create_lock = asyncio.Lock()
        self.links: dict[str, dict[str, StoreInfo]] = {}  # topic_id → linked stores
        self.usage: dict[str, set[str]] = {}  # store_id → agent names that used it

    @property
    def enabled(self) -> bool:
        return self.backend is not None

    # ------------------------------------------------------------- catalog
    async def stores(self, refresh: bool = False) -> list[StoreInfo]:
        if not self.backend:
            return []
        if self._cache and not refresh and time.time() - self._cache[0] < 30:
            return self._cache[1]
        stores = await self.backend.list()
        self._cache = (time.time(), stores)
        return stores

    async def find(self, ref: str) -> StoreInfo | None:
        ref = (ref or "").strip()
        if not ref:
            return None
        for refresh in (False, True):
            for s in await self.stores(refresh=refresh):
                if s.id == ref or s.name.lower() == ref.lower():
                    return s
        return None

    def make_name(self, region: str | None, purpose: str, taken: set[str]) -> str:
        """``al-<region>-<purpose>``, deduplicated: ``al-copilot-studio-transcript-research``."""
        p = slug(purpose, 30)
        r = slug(region or "", 22) if region else ""
        if r and p.startswith(r):  # avoid "al-copilot-studio-copilot-studio-…"
            r = ""
        base = "-".join(x for x in (self.prefix, r, p) if x)[:MAX_NAME - 3].rstrip("-")
        name, n = base, 2
        taken_l = {t.lower() for t in taken}
        while name.lower() in taken_l:
            name = f"{base}-{n}"
            n += 1
        return name

    async def create(self, purpose: str, description: str, *, region: Any = None, topic: Any = None,
                     agent: Any = None, kind: str = "mission") -> StoreInfo:
        assert self.backend is not None
        async with self._create_lock:  # parallel orchestrators must not grab the same name
            taken = {s.name for s in await self.stores(refresh=True)}
            name = self.make_name(getattr(region, "name", None), purpose, taken)
            md = {"owner": OWNER, "kind": kind, "purpose": purpose[:200], "description": description[:500],
                  "region_name": getattr(region, "name", None), "region_biome": getattr(region, "biome", None),
                  "mission_id": getattr(topic, "id", None), "mission_title": getattr(topic, "title", None),
                  "created_by": getattr(agent, "name", None), "created_by_role": getattr(agent, "role", None),
                  "created_at": int(time.time())}
            store = await self.backend.create(name, {k: v for k, v in md.items() if v is not None})
            self._cache = None
            return store

    # ------------------------------------------------------------ scoping
    def link(self, topic_id: str, store: StoreInfo) -> bool:
        linked = self.links.setdefault(topic_id, {})
        new = store.id not in linked
        linked[store.id] = store
        return new

    def linked(self, topic_id: str) -> list[StoreInfo]:
        return list(self.links.get(topic_id, {}).values())

    def resolve_linked(self, topic_id: str, ref: str) -> StoreInfo | None:
        ref = (ref or "").strip().lower()
        for s in self.linked(topic_id):
            if s.id.lower() == ref or s.name.lower() == ref:
                return s
        return None

    def touch(self, store: StoreInfo, agent: Any) -> None:
        if agent is not None:
            self.usage.setdefault(store.id, set()).add(getattr(agent, "name", "?"))

    # --------------------------------------------------- region knowledge
    def region_store_name(self, region: Any) -> str:
        return f"{self.prefix}-{slug(region.name, 40)}-knowledge"[:MAX_NAME]

    async def region_store(self, region: Any, *, create: bool = False) -> StoreInfo | None:
        if not self.backend or region is None or getattr(region, "kind", "domain") != "domain":
            return None
        name = self.region_store_name(region)
        for s in await self.stores():
            md = s.metadata or {}
            if s.name == name or (md.get("kind") == "region-knowledge" and md.get("region_name") == region.name):
                return s
        if not create:
            return None
        return await self.create(f"{region.name} knowledge", f"Shared knowledge and mission archive of the "
                                 f"'{region.name}' topic region.", region=region, kind="region-knowledge")

    async def archive(self, topic: Any, region: Any, report: str | None = None) -> StoreInfo | None:
        """Store a completed mission in the region knowledge base – the full mission report when given
        (objective, intel, every agent's input/output, tool calls), else objective + deliverable."""
        if not self.backend or not self.auto_archive or not topic.result or region is None:
            return None
        store = await self.region_store(region, create=True)
        if store is None:
            return None
        stamp = f"- Region: {region.name}\n- Round: {topic.rounds}\n- Archived: {time.strftime('%Y-%m-%d %H:%M')}\n"
        if report:
            if len(report) > MAX_ARCHIVE_CHARS:
                report = report[:MAX_ARCHIVE_CHARS] + "\n\n… (report truncated)\n"
            doc = f"{report.rstrip()}\n\n---\n\n{stamp}"
        else:
            doc = (f"# Mission: {topic.title}\n\n{stamp}\n## Objective\n\n{topic.goal}\n\n"
                   + ("## Feedback rounds\n\n" + "\n".join(f"- {f}" for f in topic.feedback) + "\n\n"
                      if topic.feedback else "")
                   + f"## Deliverable\n\n{topic.result}\n")
        await self.backend.add_documents(store.id, [(f"mission-{slug(topic.title, 40)}-r{topic.rounds}.md", doc)])
        self._cache = None
        return store

    async def region_stores(self, refresh: bool = False) -> list[StoreInfo]:
        """All non-empty region knowledge stores (the mission archive of every region)."""
        return [s for s in await self.stores(refresh=refresh)
                if (s.metadata or {}).get("kind") == "region-knowledge" and s.files]

    async def recall(self, query: str, *, limit: int = 5, per_store: int = 3, min_score: float = 0.0,
                     timeout: float = 20.0) -> list[dict[str, Any]]:
        """Search every region archive for past missions related to ``query``; best hit per document,
        each hit carries its ``store``."""
        if not self.backend or limit <= 0 or not query.strip():
            return []
        stores = await self.region_stores()
        if not stores:
            return []
        backend = self.backend

        async def one(s: StoreInfo) -> list[dict[str, Any]]:
            try:
                return [{**h, "store": s} for h in await backend.search(s.id, query[:2000], per_store)]
            except Exception as exc:  # noqa: BLE001 – one broken store must not block the recall
                log.warning("Recall in %s failed: %s", s.name, exc)
                return []

        results = await asyncio.wait_for(asyncio.gather(*(one(s) for s in stores)), timeout)
        hits = sorted((h for r in results for h in r if float(h.get("score") or 0) >= min_score),
                      key=lambda h: -float(h.get("score") or 0))
        out: list[dict[str, Any]] = []
        seen: set[tuple[str, Any]] = set()
        for h in hits:
            key = (h["store"].id, h.get("filename"))
            if key not in seen:
                seen.add(key)
                out.append(h)
            if len(out) >= limit:
                break
        return out

    async def add(self, store: StoreInfo, docs: list[tuple[str, str]]) -> None:
        assert self.backend is not None
        if not store.owned:
            raise PermissionError(f"'{store.name}' was not created by Agent Landscape – it is read-only for agents")
        await self.backend.add_documents(store.id, docs)
        self._cache = None

    async def close(self) -> None:
        if self.backend:
            await self.backend.close()

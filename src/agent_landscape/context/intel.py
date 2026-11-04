"""Mission context ("intel"): what the commander hands to the team besides the objective.

Wire format (A2A-native, so any A2A client – e.g. LiteLLM's Agent Gateway – can attach context):
  * files   → ``FilePart``  {"kind": "file", "file": {"name", "mimeType", "bytes": <base64>} | {"uri"}}
  * others  → ``DataPart``  {"kind": "data", "data": {"type": "agent-landscape/context", "items": [...]}}
    item kinds: {"kind": "path", "path"} · {"kind": "url", "url"} · {"kind": "tool", "tool": "server[/tool]"}
               · {"kind": "note", "name", "content"} · {"kind": "store", "store": "<knowledge base name|id>"}
"""

from __future__ import annotations

import base64
import html
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from ..a2a.types import DataPart, FilePart, Message, Part, TextPart
from . import sandbox
from .mcp import MCPGateway, MCPTool

CONTEXT_TYPE = "agent-landscape/context"
INLINE_BUDGET = 4000  # chars of attached docs inlined into every briefing; the rest via read_intel
MAX_DOC_CHARS = 400_000


class ContextItem(BaseModel):
    kind: Literal["file", "path", "url", "tool", "note", "store"]
    name: str | None = Field(default=None, max_length=300)
    content: str | None = Field(default=None, max_length=2_000_000)  # file / note text
    mime: str | None = None
    path: str | None = Field(default=None, max_length=2000)
    url: str | None = Field(default=None, max_length=2000)
    tool: str | None = Field(default=None, max_length=300)
    store: str | None = Field(default=None, max_length=300)  # knowledge base name or id


@dataclass
class Doc:
    name: str
    text: str
    source: str  # file | url | note | path
    origin: str = ""

    @property
    def size(self) -> int:
        return len(self.text)


@dataclass
class MissionIntel:
    docs: dict[str, Doc] = field(default_factory=dict)
    roots: list[Path] = field(default_factory=list)
    tools: list[MCPTool] = field(default_factory=list)
    stores: list[Any] = field(default_factory=list)  # vectorstore.StoreInfo picked by the user
    summary: list[dict[str, Any]] = field(default_factory=list)  # for UI / reports (incl. errors)

    @property
    def empty(self) -> bool:
        return not (self.docs or self.roots or self.tools)

    def add_doc(self, doc: Doc) -> Doc:
        name, n = doc.name, 2
        while name in self.docs:
            stem, dot, ext = doc.name.rpartition(".")
            name = f"{stem} ({n}).{ext}" if dot else f"{doc.name} ({n})"
            n += 1
        doc.name = name
        doc.text = doc.text[:MAX_DOC_CHARS]
        self.docs[name] = doc
        return doc

    def briefing(self) -> str:
        """Text appended to every mission agent's system prompt."""
        if self.empty:
            return ""
        out = ["## Mission intel (context attached by the commander)"]
        budget = INLINE_BUDGET
        if self.docs:
            out.append("### Documents")
            for d in self.docs.values():
                if d.size <= budget:
                    budget -= d.size
                    out.append(f"--- {d.name} ({d.source}) ---\n{d.text}\n--- end of {d.name} ---")
                else:
                    preview = " ".join(d.text[:300].split())
                    out.append(f"- **{d.name}** ({d.source}, {d.size:,} chars) – preview: {preview}… "
                               f"→ call `read_intel` with name=\"{d.name}\" to read it.")
        if self.roots:
            out.append("### Folders (read-only; use list_dir / read_file / search_files)")
            for r in self.roots:
                out.append(f"{r}/\n{sandbox.tree(r, max_entries=40)}")
        if self.tools:
            out.append("### External tools (via LiteLLM MCP gateway)")
            out += [f"- {t.server}/{t.name}: {t.description[:160]}" for t in self.tools]
        out.append("Use the tools when the task needs facts from this intel – don't guess file contents.")
        return "\n\n".join(out)


# ------------------------------------------------------------------ encoding
def items_to_parts(goal: str, items: list[ContextItem]) -> list[Part]:
    parts: list[Part] = [TextPart(text=goal)]
    refs = []
    for it in items:
        if it.kind == "file" and it.content is not None:
            parts.append(FilePart(file={"name": it.name or "attachment.txt", "mimeType": it.mime or "text/plain",
                                        "bytes": base64.b64encode(it.content.encode()).decode()}))
        else:
            refs.append(it.model_dump(exclude_none=True))
    if refs:
        parts.append(DataPart(data={"type": CONTEXT_TYPE, "items": refs}))
    return parts


def items_from_message(msg: Message) -> list[ContextItem]:
    items: list[ContextItem] = []
    for p in msg.parts:
        if isinstance(p, FilePart):
            f = p.file or {}
            if f.get("bytes"):
                try:
                    raw = base64.b64decode(f["bytes"])
                except Exception:  # noqa: BLE001
                    continue
                if b"\0" in raw[:8192]:
                    items.append(ContextItem(kind="note", name=f.get("name") or "binary",
                                             content=f"(binary attachment {f.get('name')} – {len(raw)} bytes – not readable)"))
                    continue
                items.append(ContextItem(kind="file", name=f.get("name"), mime=f.get("mimeType"),
                                         content=raw.decode("utf-8", errors="replace")))
            elif f.get("uri"):
                items.append(ContextItem(kind="url", url=f["uri"], name=f.get("name")))
        elif isinstance(p, DataPart):
            data = p.data or {}
            if data.get("type") == CONTEXT_TYPE:
                for raw_item in data.get("items") or []:
                    try:
                        items.append(ContextItem.model_validate(raw_item))
                    except Exception:  # noqa: BLE001
                        continue
    return items


# ---------------------------------------------------------------- resolution
_TAGS = re.compile(r"<(script|style|noscript|svg)[^>]*>.*?</\1>|<[^>]+>", re.S | re.I)


def html_to_text(raw: str) -> str:
    text = _TAGS.sub(" ", raw)
    text = html.unescape(text)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()


async def fetch_url(url: str, http: httpx.AsyncClient) -> tuple[str, str]:
    if not re.match(r"^https?://", url, re.I):
        raise ValueError("Only http(s) URLs are supported")
    r = await http.get(url, timeout=15, follow_redirects=True, headers={"User-Agent": "agent-landscape/0.1"})
    r.raise_for_status()
    ctype = r.headers.get("content-type", "")
    body = r.content[:3_000_000]
    if not any(t in ctype for t in ("text", "json", "xml", "yaml", "javascript")) and b"\0" in body[:4096]:
        raise ValueError(f"Unsupported content type {ctype or 'binary'}")
    text = body.decode(r.encoding or "utf-8", errors="replace")
    title_m = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
    if "html" in ctype:
        text = html_to_text(text)
    return (html.unescape(title_m.group(1).strip()) if title_m else url), text


async def resolve(items: list[ContextItem], *, allowed_roots: list[str], http: httpx.AsyncClient,
                  mcp: MCPGateway, offline: bool = False, vectors: Any = None) -> MissionIntel:
    intel = MissionIntel()
    tool_refs: list[str] = []
    for it in items:
        entry: dict[str, Any] = {"kind": it.kind}
        try:
            if it.kind in ("file", "note"):
                doc = intel.add_doc(Doc(name=it.name or ("note" if it.kind == "note" else "attachment.txt"),
                                        text=it.content or "", source=it.kind))
                entry.update(name=doc.name, size=doc.size, detail=f"{doc.size:,} chars")
            elif it.kind == "path":
                p = sandbox.check_allowed(it.path or "", allowed_roots)
                info = sandbox.inspect(p)
                if p.is_dir():
                    if p not in intel.roots:
                        intel.roots.append(p)
                    entry.update(name=str(p), detail=f"folder · {info['files']}{'+' if info['files_capped'] else ''} files · {info['size_h']}")
                else:
                    if not info["text"]:
                        raise sandbox.SandboxError(f"{p.name} is a binary file")
                    if info["too_big"]:
                        raise sandbox.SandboxError(f"{p.name} is too large ({info['size_h']})")
                    doc = intel.add_doc(Doc(name=p.name, text=p.read_text(errors="replace"), source="path", origin=str(p)))
                    entry.update(name=str(p), size=doc.size, detail=f"file · {info['size_h']}")
            elif it.kind == "url":
                if offline:
                    raise ValueError("URL fetching is disabled in this mode")
                title, text = await fetch_url(it.url or "", http)
                doc = intel.add_doc(Doc(name=it.name or title[:80], text=text, source="url", origin=it.url or ""))
                entry.update(name=it.url, size=doc.size, detail=f"web page · {doc.size:,} chars · “{title[:60]}”")
            elif it.kind == "tool":
                tool_refs.append(it.tool or "")
                entry.update(name=it.tool)
            elif it.kind == "store":
                if vectors is None or not vectors.enabled:
                    raise ValueError("No vector store service configured (VECTOR_STORE_URL)")
                store = await vectors.find(it.store or "")
                if store is None:
                    raise ValueError(f"Knowledge base '{it.store}' not found")
                intel.stores.append(store)
                entry.update(name=store.name, detail=f"knowledge base · {store.files} files"
                             + ("" if store.owned else " · read-only"))
            entry["ok"] = True
        except Exception as exc:  # noqa: BLE001 – report per item, never fail the mission
            entry.update(name=it.name or it.path or it.url or it.tool or it.store, ok=False, error=str(exc)[:300])
        intel.summary.append(entry)
    if tool_refs:
        intel.tools = await mcp.select(tool_refs)
        found = {t.server for t in intel.tools} | {t.ref for t in intel.tools}
        for e in intel.summary:
            if e["kind"] == "tool" and e.get("ok"):
                n = sum(1 for t in intel.tools if t.ref == e["name"] or t.server == e["name"])
                if e["name"] not in found:
                    e.update(ok=False, error="tool / server not found on the LiteLLM MCP gateway")
                else:
                    e["detail"] = f"{n} tool{'s' if n != 1 else ''} via LiteLLM MCP"
    return intel

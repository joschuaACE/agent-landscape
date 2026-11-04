"""Topic regions ("areas") the Command HQ charts on the map – each with its own biome."""

from __future__ import annotations

import json
import re
import time
import zlib
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Biome:
    key: str
    label: str
    icon: str
    hint: str
    keywords: tuple[str, ...] = ()


BIOMES: dict[str, Biome] = {
    b.key: b
    for b in [
        Biome("capital", "Capital Plains", "🏰", "the Command HQ"),
        Biome("meadow", "Sunflower Fields", "🌻",
              "greenfield work: new concepts, new platforms or products, ideation, visions, roadmaps",
              ("concept", "greenfield", "new platform", "platform", "vision", "ideation", "idea", "roadmap",
               "strategy", "innovation", "mvp", "prototype")),
        Biome("desert", "Desert", "🏜️",
              "configuring & integrating existing vendor / low-code platforms (Copilot Studio, Power Platform, "
              "SharePoint, Salesforce …) – dry, constrained terrain",
              ("copilot studio", "power platform", "power automate", "power apps", "low-code", "lowcode",
               "sharepoint", "dynamics", "salesforce", "servicenow", "outlook", "teams", "graph api", "connector",
               "saas")),
        Biome("crystal", "Crystal Caves", "🔮",
              "AI/ML engineering: LLMs, prompts, RAG, embeddings, evaluation, fine-tuning",
              (" llm", "genai", "gen ai", "prompt", " rag ", "embedding", "fine-tun", "machine learning", " ml ",
               " ai ")),
        Biome("metropolis", "Metropolis", "🏙️",
              "infrastructure & backend: cloud, DevOps, APIs, databases, scaling, platform engineering",
              ("kubernetes", "k8s", "cloud", "devops", "infrastructure", "database", " api", "backend", "deploy",
               "ci/cd", "pipeline", "scaling", "server")),
        Biome("forest", "Deep Forest", "🌲",
              "research & knowledge: analysis, documentation, learning, market or literature research",
              ("research", "analys", "documentation", "knowledge", "study", "compare", "survey", "market")),
        Biome("volcanic", "Volcano", "🌋", "firefighting: incidents, outages, security, risks, crises",
              ("incident", "outage", "security", "vulnerab", "breach", "post-mortem", "postmortem", "risk",
               "crisis", "hotfix")),
        Biome("tundra", "Frozen Tundra", "❄️", "governance: compliance, legal, policies, audits, licensing",
              ("compliance", "gdpr", "legal", "policy", "governance", "audit", "license", "regulation",
               "contract")),
        Biome("swamp", "Swamp", "🐸", "legacy & tech debt: migrations, refactoring, old systems",
              ("legacy", "migration", "migrate", "refactor", "tech debt", "technical debt", "monolith", "upgrade")),
        Biome("tropical", "Tropical Isles", "🌴",
              "go-to-market: marketing, sales, customers, growth, launches, events",
              ("marketing", "sales", "customer", "launch", "go-to-market", "growth", "campaign", "brand", "event",
               "newsletter")),
        Biome("autumn", "Autumn Woods", "🍁",
              "people & communication: HR, onboarding, mails, meetings, writing, team topics",
              (" mail", "email", " hr ", "onboarding", "hiring", "meeting", "retro", "chef", "boss", "letter",
               "presentation")),
        Biome("highlands", "Highlands", "⛰️",
              "architecture & big decisions: system design, long-term planning, trade-offs",
              ("architecture", "system design", "decision", "trade-off", "tradeoff", "long-term", "evaluate")),
    ]
}
DOMAIN_BIOMES = [k for k in BIOMES if k != "capital"]


def guess_biome(text: str) -> str:
    """Keyword heuristic (mock mode / fallback when the LLM returns an unknown biome)."""
    t = f" {' '.join(text.lower().split())} "
    best, best_score = None, 0
    for key in DOMAIN_BIOMES:
        score = sum(kw in t for kw in BIOMES[key].keywords)
        if score > best_score:
            best, best_score = key, score
    return best or DOMAIN_BIOMES[zlib.crc32(t.encode()) % len(DOMAIN_BIOMES)]


@dataclass
class Region:
    id: str
    name: str
    description: str
    biome: str
    index: int  # slot on the region lattice (0 = HQ)
    center: tuple[int, int]  # cell of the region's central territory
    kind: str = "domain"  # domain | hq
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        b = BIOMES.get(self.biome, BIOMES["meadow"])
        return {"id": self.id, "name": self.name, "description": self.description, "biome": self.biome,
                "biome_label": b.label, "icon": b.icon, "index": self.index, "kind": self.kind,
                "q": self.center[0], "r": self.center[1], "created_at": self.created_at}


# ------------------------------------------------------------------ survey
def survey_system(regions: list[dict[str, Any]]) -> str:
    if regions:
        lines = "\n".join(
            f'- id="{r["id"]}" name="{r["name"]}" ({r["biome"]}): {r["description"]}'
            + (f' | missions: {"; ".join(r["missions"])}' if r["missions"] else "")
            for r in regions)
    else:
        lines = "(none yet)"
    biomes = "\n".join(f"- {k}: {BIOMES[k].label} – {BIOMES[k].hint}" for k in DOMAIN_BIOMES)
    return (
        "You are the High Commander of an agent landscape map. Missions are grouped into topic regions: one region "
        "per product, project or domain, so related missions are worked on in the same area of the map.\n"
        "Decide whether the new mission belongs to an EXISTING region (same product / project / domain) or opens "
        "a NEW region. Prefer an existing region when the topic clearly matches.\n\n"
        f"Existing regions:\n{lines}\n\n"
        f"Biomes for new regions (pick the one whose character fits the topic):\n{biomes}\n\n"
        'Respond ONLY with JSON, either {"region_id": "<existing id>"} or {"new_region": {"name": "<2-4 word '
        'topic/project name, e.g. Copilot Studio, GenAI Platform>", "description": "<one sentence>", '
        '"biome": "<biome key>"}}'
    )


_STOP = {"with", "that", "this", "from", "into", "your", "what", "when", "make", "build", "write", "create",
         "plan", "about", "some", "have", "will", "should", "could", "would", "their", "there", "which",
         "mission", "topic", "task", "using", "need", "needs", "please", "short", "give"}


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 3 and w not in _STOP}


def mock_survey(title: str, goal: str, regions: list[dict[str, Any]]) -> str:
    words = _tokens(f"{title} {goal}")
    best, best_hits = None, 0
    for r in regions:
        rt = _tokens(r["name"])
        hits = len(rt & words)
        if rt and hits >= max(1, (len(rt) + 1) // 2) and hits > best_hits:
            best, best_hits = r["id"], hits
    if best:
        return json.dumps({"region_id": best})
    name = " ".join(title.split()[:3]) or "New Frontier"
    return json.dumps({"new_region": {"name": name, "description": " ".join(goal.split())[:160],
                                      "biome": guess_biome(f"{title} {goal}")}})


def parse_survey(raw: str) -> dict[str, Any]:
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}

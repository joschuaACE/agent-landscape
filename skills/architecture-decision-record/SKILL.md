---
name: architecture-decision-record
description: Write an Architecture Decision Record (ADR) – use when a task asks to decide between options, document a technical decision or justify an architecture choice.
---
# Architecture Decision Record

An ADR captures **one** significant decision, its context and its consequences so that later readers
understand *why* the system looks the way it does.

## Steps
1. Name the decision as a short imperative phrase ("Use SQLite for local persistence").
2. Describe the **context**: forces, constraints, requirements, what triggered the decision.
3. List **at least two options** that were seriously considered – including "do nothing" if relevant.
   For each: one-line summary, pros, cons.
4. State the **decision** clearly and link it to the forces from the context.
5. Spell out the **consequences** – positive, negative, and follow-up work. Be honest about trade-offs.
6. Set a **status**: proposed, accepted, deprecated or superseded (by ADR-xxxx).

## Rules
- One decision per ADR. Split if you find yourself writing "and also".
- Keep it under one page; link to details instead of pasting them.
- Use facts from your intel / knowledge base and cite the source names.
- Use the exact structure from `template.md` (call `read_skill_file` with `template.md`).

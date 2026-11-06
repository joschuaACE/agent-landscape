"""Base agent: an A2A executor with LLM access, status reporting and delegation."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import math
import time
import uuid
from typing import TYPE_CHECKING, Any

from ..a2a.task_manager import TaskContext
from ..a2a.types import (
    TERMINAL_STATES,
    AgentCapabilities,
    AgentCard,
    AgentProvider,
    AgentSkill,
    Message,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatusUpdateEvent,
    parts_text,
    result_text,
)

if TYPE_CHECKING:
    from ..runtime.runtime import Runtime

# headers (X-LiteLLM-Trace-Id, ...) of the A2A request currently being served
_litellm_headers: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar("litellm_headers", default={})
# LLM usage of the A2A task currently being served (→ task metadata, mission reports)
_task_usage: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("task_usage", default=None)

ACTIVE_STATES = {"working", "thinking", "delegating", "waiting"}


def short(text: str, n: int = 160) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


class Agent:
    role: str = "agent"
    icon: str = "🤖"
    title: str = "Agent"
    description: str = "A generic agent."
    persistent: bool = False  # persistent agents return to idle instead of "done"

    def __init__(
        self,
        runtime: "Runtime",
        *,
        name: str,
        topic_id: str,
        parent_id: str | None = None,
        model: str | None = None,
        depth: int = 0,
        mode: str = "auto",
        agent_id: str | None = None,
        **options: Any,
    ) -> None:
        self.runtime = runtime
        self.id = agent_id or f"{self.role.replace('_', '-')}-{uuid.uuid4().hex[:8]}"
        self.name = name
        self.topic_id = topic_id
        self.parent_id = parent_id
        self.model = model  # assigned by the mission / parent (re-set on every re-deployment)
        self.pinned_model: str | None = None  # set by a human in the Unit panel – wins over everything
        self.depth = depth
        self.mode = mode
        self.options = options
        self.children: list[str] = []
        self.status = "idle"
        self.activity = "Awaiting orders"
        self.tokens = 0
        self.cost = 0.0
        self.xp = 0
        self.tasks_done = 0
        self.last_input: str | None = None
        self.last_output: str | None = None
        self.objective = ""
        self.memory: list[dict[str, str]] = []  # service record: earlier tasks + results (continuity on reuse)
        self.recruitments = 0
        self.profile_key: str | None = getattr(self, "profile_key", None)  # may be set by subclasses first
        self.skills_used: list[str] = []
        self.position: tuple[int, int] = (0, 0)
        self._inflight = 0
        self.created_at = time.time()
        self.updated_at = self.created_at

    # ------------------------------------------------------------ profile
    @property
    def profile(self) -> Any:
        """Live profile (edits on disk / in the UI apply to the next LLM turn); None for profile-less units."""
        reg = getattr(self.runtime, "profiles", None)
        return reg.get(self.profile_key) if reg is not None and self.profile_key else None

    # ---------------------------------------------------------------- A2A
    @property
    def url(self) -> str:
        return f"{self.runtime.settings.public_url}/a2a/{self.id}"

    def skills(self) -> list[AgentSkill]:
        return [AgentSkill(id=self.role, name=self.title, description=self.description, tags=[self.role])]

    def card(self) -> AgentCard:
        return AgentCard(
            name=self.name,
            description=self.description,
            url=self.url,
            provider=AgentProvider(organization=self.runtime.settings.organization,
                                   url=self.runtime.settings.public_url),
            capabilities=AgentCapabilities(streaming=True),
            skills=self.skills(),
        )

    async def execute(self, ctx: TaskContext) -> None:
        token = _litellm_headers.set(ctx.litellm_headers)
        usage: dict[str, Any] = {"tokens": 0, "cost": 0.0, "model": None, "topic_id": ctx.task.context_id}
        usage_token = _task_usage.set(usage)
        ctx.task.metadata = {**(ctx.task.metadata or {}), "agent_name": self.name, "role": self.role,
                             "icon": self.icon, "title": self.title,
                             "parent_id": ctx.metadata.get("parent_id"), "subtask": ctx.metadata.get("subtask")}
        prof = self.profile
        if prof is not None:
            ctx.task.metadata.update(profile=prof.key, profile_rev=prof.rev, profile_source=prof.source)
        self.skills_used = []
        self.mode = ctx.metadata.get("mode", self.mode)
        self.last_input = ctx.text
        self.objective = str(ctx.metadata.get("subtask") or "") or short(ctx.text, 60)
        self._inflight += 1
        self.set_status("working", "Orders received")
        try:
            result = await self.run(ctx.text, ctx)
            self.last_output = result
            if not self.persistent:
                self.memory = [*self.memory, {"task": self.objective, "result": short(result, 700)}][-3:]
            self.tasks_done += 1
            self.gain_xp(50)
            await ctx.add_artifact("result", result)
            await ctx.complete(short(result, 280))
            if not self.persistent:
                self.set_status("done", "Mission accomplished")
        except asyncio.CancelledError:
            if not self.persistent:
                self.set_status("canceled", "Recalled")
            raise
        except Exception as exc:
            self.runtime.log(f"{self.name} failed: {exc}", agent=self, level="error")
            if not self.persistent:
                self.set_status("failed", short(str(exc), 120))
            raise
        finally:
            _litellm_headers.reset(token)
            _task_usage.reset(usage_token)
            ctx.task.metadata.update(tokens=usage["tokens"], cost=round(usage["cost"], 6), model=usage["model"],
                                     tool_calls=usage.get("tool_calls", []), skills_used=list(self.skills_used),
                                     finished_at=time.time())
            self._inflight -= 1
            if self.persistent and self._inflight == 0:
                self.rest()

    async def run(self, text: str, ctx: TaskContext) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    def rest(self) -> None:
        """Status of a persistent unit between jobs."""
        self.set_status("idle", "Awaiting orders")

    # ------------------------------------------------------------ helpers
    @property
    def level(self) -> int:
        return 1 + int(math.sqrt(self.xp / 40))

    def gain_xp(self, amount: int) -> None:
        before = self.level
        self.xp += max(0, int(amount))
        if self.level > before:
            self.runtime.emit("level_up", agent_id=self.id, level=self.level)

    def set_status(self, status: str, activity: str | None = None) -> None:
        self.status = status
        if activity is not None:
            self.activity = activity
        self.updated_at = time.time()
        self.runtime.agent_changed(self)

    async def think(self, system: str, prompt: str, *, activity: str, mock: str | None = None,
                    temperature: float | None = None, model: str | None = None, use_tools: bool = True) -> str:
        """One LLM turn – with a tool loop when the mission has intel/tools attached.

        Model: pinned (human, Unit panel) > explicit > mission model (self.model) > profile model > DEFAULT_MODEL."""
        prof = self.profile
        if temperature is None:
            temperature = prof.temperature if prof is not None and prof.temperature is not None else 0.7
        model = self.pinned_model or model or self.model or (prof.model if prof is not None else None)
        prev = (self.status, self.activity)
        self.set_status("thinking", activity)
        if self.memory and not self.persistent:
            system += ("\n\n---\nYour service record (your earlier assignments – build on them for continuity):\n"
                       + "\n".join(f"• Task: {m['task']}\n  Your result: {m['result']}" for m in self.memory))
        toolbox = None if self.persistent else self.runtime.toolbox_for(self.topic_id)
        if toolbox is not None:
            brief = toolbox.briefing(self, with_tools=use_tools)
            if brief:
                system += "\n\n---\n" + brief
        if not use_tools:
            toolbox = None
        if toolbox is not None:
            try:
                await toolbox.prepare(self)  # MCP tools granted by the profile
            except Exception as exc:  # noqa: BLE001 – a missing MCP server must not stop the agent
                self.runtime.log(f"{self.name}: granted MCP tools unavailable ({exc})", agent=self, level="warn")
        tools = toolbox.specs(self) if toolbox else []
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        max_steps = (prof.max_tool_steps if prof is not None and prof.max_tool_steps
                     else self.runtime.settings.max_tool_steps)

        if tools and self.runtime.settings.mock:  # exercise the tool plumbing without an LLM
            for name, args in toolbox.mock_plan(self):  # type: ignore[union-attr]
                await self._run_tool(toolbox, name, args, activity)
            tools = []

        steps = 0
        while True:
            res = await self.runtime.llm.complete(
                messages,
                model=model or self.model,
                mock=mock,
                temperature=temperature,
                headers=_litellm_headers.get() or None,
                metadata={"agent_id": self.id, "topic_id": self.topic_id},
                tools=tools or None,
            )
            self._account(res)
            if not res.tool_calls or not toolbox or steps >= max_steps + 1:
                break
            messages.append(res.assistant_message())
            for tc in res.tool_calls:
                if steps >= max_steps:
                    result = "Tool budget exhausted – give your final answer now with the information you have."
                else:
                    result = await self._run_tool(toolbox, tc.name, tc.arguments, activity)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            steps += 1
            tools = toolbox.specs(self)  # e.g. a knowledge base created this turn becomes searchable
            if steps == max_steps:
                messages.append({"role": "user", "content": "You have used your tool budget. "
                                 "Answer now, without calling more tools."})
            self.set_status("thinking", activity)
        self.set_status("working" if prev[0] in ("thinking", "idle") else prev[0], prev[1])
        return res.text or "(no answer – the tool budget ran out)"

    async def _run_tool(self, toolbox: Any, name: str, args: Any, activity: str) -> str:
        label = toolbox.label(name, args if isinstance(args, dict) else {})
        self.set_status("working", f"🔧 {label}")
        self.runtime.emit("tool_call", agent_id=self.id, tool=name, label=label)
        t0 = time.perf_counter()
        result, ok, parsed = await toolbox.call(name, args, self)
        usage = _task_usage.get()
        if usage is not None:
            usage.setdefault("tool_calls", []).append({
                "name": name, "label": label, "args": parsed, "ok": ok,
                "ms": round((time.perf_counter() - t0) * 1000), "result": result[:1500]})
        self.gain_xp(5)
        if self.runtime.settings.mock:
            await asyncio.sleep(min(0.25, self.runtime.settings.mock_delay[1]))
        return result

    def _account(self, res: Any) -> None:
        self.tokens += res.tokens
        self.cost += res.cost
        self.gain_xp(res.tokens // 20)
        usage = _task_usage.get()
        if usage is not None:
            usage["tokens"] += res.tokens
            usage["cost"] += res.cost
            usage["model"] = res.model
        self.runtime.add_usage(res.tokens, res.cost, topic_id=usage["topic_id"] if usage else self.topic_id)

    def spawn(self, cls: type["Agent"], **kwargs: Any) -> "Agent":
        kwargs.setdefault("mode", self.mode)
        kwargs.setdefault("model", None)
        child = self.runtime.spawn(cls, topic_id=kwargs.pop("topic_id", self.topic_id), parent=self, **kwargs)
        return child

    async def delegate(self, child: "Agent", text: str, *, metadata: dict[str, Any] | None = None) -> str:
        """Send an A2A ``message/stream`` to ``child`` and return its final answer.

        Cancellation propagates: if this coroutine is cancelled the child task is
        cancelled via ``tasks/cancel``.
        """
        md = {"topic_id": self.topic_id, "parent_id": self.id, "mode": self.mode, **(metadata or {})}
        msg = Message.of(text, role="user", context_id=md["topic_id"], metadata=md)
        client = self.runtime.client_for(child)
        self.runtime.emit_message(self.id, child.id, "request", text)
        self.gain_xp(10)

        task_id: str | None = None
        final_state: TaskState | None = None
        final_text = ""
        artifacts: list[str] = []
        try:
            async for ev in client.stream(msg):
                if isinstance(ev, Task):
                    task_id = ev.id
                    if ev.status.state in TERMINAL_STATES:  # finished before the stream started → snapshot only
                        final_state = ev.status.state
                        final_text = result_text(ev)
                        artifacts = [parts_text(a.parts) for a in ev.artifacts]
                elif isinstance(ev, TaskArtifactUpdateEvent):
                    artifacts.append(parts_text(ev.artifact.parts))
                elif isinstance(ev, TaskStatusUpdateEvent) and ev.final:
                    final_state = ev.status.state
                    final_text = result_text(ev)
                elif isinstance(ev, Message):
                    final_state, final_text = TaskState.completed, ev.text
        except asyncio.CancelledError:
            if task_id:
                with contextlib.suppress(Exception):
                    await asyncio.shield(client.cancel(task_id))
            raise
        answer = "\n\n".join(a for a in artifacts if a) or final_text
        if final_state != TaskState.completed:
            self.runtime.emit_message(child.id, self.id, "error", final_text or str(final_state))
            raise RuntimeError(f"{child.name} ended with {final_state.value if final_state else 'no result'}: "
                               f"{final_text}")
        self.runtime.emit_message(child.id, self.id, "response", answer)
        return answer

    # --------------------------------------------------------------- view
    def model_source(self) -> tuple[str | None, str]:
        """(model, where it comes from) as used by the next LLM turn (orchestrators may still fall back to
        ORCHESTRATOR_MODEL instead of DEFAULT_MODEL)."""
        prof = self.profile
        if self.pinned_model:
            return self.pinned_model, "pinned"
        if self.model:
            return self.model, "mission"
        if prof is not None and prof.model:
            return prof.model, "profile"
        return None, "default"

    def to_dict(self) -> dict[str, Any]:
        model, source = self.model_source()
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "title": self.title,
            "icon": self.icon,
            "topic_id": self.topic_id,
            "parent_id": self.parent_id,
            "children": list(self.children),
            "depth": self.depth,
            "status": self.status,
            "objective": self.objective or None,
            "activity": self.activity,
            "model": self.runtime.llm.resolve_model(model),
            "model_source": source,
            "pinned_model": self.pinned_model,
            "tokens": self.tokens,
            "cost": round(self.cost, 6),
            "xp": self.xp,
            "level": self.level,
            "tasks_done": self.tasks_done,
            "profile": self.profile_key,
            "profile_rev": self.profile.rev if self.profile is not None else None,
            "recruitments": self.recruitments,
            "veteran": self.tasks_done >= 2 and not self.persistent,
            "memory": [{"task": m["task"], "result": short(m["result"], 300)} for m in self.memory],
            "q": self.position[0],
            "r": self.position[1],
            "url": self.url,
            "persistent": self.persistent,
            "last_input": short(self.last_input or "", 600) or None,
            "last_output": (self.last_output or "")[:4000] or None,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

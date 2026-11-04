"""In-memory A2A task store + executor runner with pub/sub for streaming."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Any, Callable, Protocol

from .types import (
    TERMINAL_STATES,
    Artifact,
    Message,
    MessageSendParams,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    TextPart,
    new_id,
)

log = logging.getLogger(__name__)


class Executor(Protocol):
    async def execute(self, ctx: "TaskContext") -> None: ...


class TaskContext:
    """Handle given to an executor to report progress on one A2A task."""

    def __init__(
        self,
        manager: "TaskManager",
        task: Task,
        message: Message,
        params_metadata: dict[str, Any] | None,
        headers: dict[str, str] | None,
    ) -> None:
        self.manager = manager
        self.task = task
        self.message = message
        self.metadata: dict[str, Any] = {**(params_metadata or {}), **(message.metadata or {})}
        self.headers = headers or {}

    @property
    def text(self) -> str:
        return self.message.text

    @property
    def litellm_headers(self) -> dict[str, str]:
        """X-LiteLLM-* headers to forward to LLM calls (trace + spend attribution)."""
        return {k: v for k, v in self.headers.items() if k.lower().startswith("x-litellm-")}

    @property
    def is_terminal(self) -> bool:
        return self.task.status.state in TERMINAL_STATES

    async def update(self, state: TaskState, text: str | None = None, *, final: bool = False) -> None:
        msg = None
        if text:
            msg = Message.of(text, role="agent", task_id=self.task.id, context_id=self.task.context_id)
            self.task.history.append(msg)
        self.task.status = TaskStatus(state=state, message=msg)
        await self.manager.publish(
            self.task.id,
            TaskStatusUpdateEvent(
                task_id=self.task.id,
                context_id=self.task.context_id,
                status=self.task.status,
                final=final or state in TERMINAL_STATES,
            ),
        )

    async def working(self, text: str | None = None) -> None:
        await self.update(TaskState.working, text)

    async def add_artifact(self, name: str, text: str, metadata: dict[str, Any] | None = None) -> Artifact:
        art = Artifact(name=name, parts=[TextPart(text=text)], metadata=metadata)
        self.task.artifacts.append(art)
        await self.manager.publish(
            self.task.id,
            TaskArtifactUpdateEvent(
                task_id=self.task.id, context_id=self.task.context_id, artifact=art, last_chunk=True
            ),
        )
        return art

    async def complete(self, text: str | None = None) -> None:
        await self.update(TaskState.completed, text, final=True)

    async def fail(self, text: str) -> None:
        await self.update(TaskState.failed, text, final=True)


class TaskManager:
    def __init__(self, max_tasks: int = 5000) -> None:
        self.tasks: OrderedDict[str, Task] = OrderedDict()
        self.runners: dict[str, asyncio.Task] = {}
        self.subscribers: dict[str, set[asyncio.Queue]] = {}
        self.max_tasks = max_tasks
        self.listener: Callable[[str], None] | None = None  # called with the task id on every change

    def _notify(self, task_id: str) -> None:
        if self.listener is not None:
            self.listener(task_id)

    # -- store --------------------------------------------------------------
    def get(self, task_id: str) -> Task | None:
        return self.tasks.get(task_id)

    def list(self, context_id: str | None = None, agent_id: str | None = None) -> list[Task]:
        out = list(self.tasks.values())
        if context_id:
            out = [t for t in out if t.context_id == context_id]
        if agent_id:
            out = [t for t in out if (t.metadata or {}).get("agent_id") == agent_id]
        return out

    def _evict(self) -> None:
        while len(self.tasks) > self.max_tasks:
            for tid, t in self.tasks.items():
                if t.status.state in TERMINAL_STATES:
                    del self.tasks[tid]
                    break
            else:
                return

    # -- pub/sub ------------------------------------------------------------
    def subscribe(self, task_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.subscribers.setdefault(task_id, set()).add(q)
        return q

    def unsubscribe(self, task_id: str, q: asyncio.Queue) -> None:
        subs = self.subscribers.get(task_id)
        if subs:
            subs.discard(q)
            if not subs:
                self.subscribers.pop(task_id, None)

    async def publish(self, task_id: str, event: Any) -> None:
        self._notify(task_id)
        for q in list(self.subscribers.get(task_id, ())):
            q.put_nowait(event)

    # -- execution ----------------------------------------------------------
    def start(
        self,
        executor: Executor,
        params: MessageSendParams,
        *,
        agent_id: str,
        headers: dict[str, str] | None = None,
    ) -> tuple[Task, asyncio.Task]:
        """Create (or continue) a task and schedule the executor.

        Synchronous on purpose: callers can subscribe before the runner starts.
        """
        msg = params.message
        task = self.tasks.get(msg.task_id) if msg.task_id else None
        if task is None or task.status.state in TERMINAL_STATES:
            task = Task(
                context_id=msg.context_id or (task.context_id if task else None) or new_id(),
                status=TaskStatus(state=TaskState.submitted),
                metadata={"agent_id": agent_id, "created_at": time.time()},
            )
            self.tasks[task.id] = task
            self._evict()
        msg.task_id, msg.context_id = task.id, task.context_id
        task.history.append(msg)
        self._notify(task.id)
        ctx = TaskContext(self, task, msg, params.metadata, headers)
        runner = asyncio.create_task(self._run(executor, ctx), name=f"a2a-task-{task.id}")
        self.runners[task.id] = runner
        return task, runner

    async def _run(self, executor: Executor, ctx: TaskContext) -> None:
        try:
            await ctx.working()
            await executor.execute(ctx)
            if not ctx.is_terminal:
                await ctx.complete()
        except asyncio.CancelledError:
            if not ctx.is_terminal:
                await ctx.update(TaskState.canceled, "Task canceled", final=True)
        except Exception as exc:  # noqa: BLE001
            log.exception("Task %s failed", ctx.task.id)
            if not ctx.is_terminal:
                await ctx.fail(f"{type(exc).__name__}: {exc}")
        finally:
            self.runners.pop(ctx.task.id, None)
            self._notify(ctx.task.id)  # final metadata (tokens, tool calls) is written after the last event

    async def cancel(self, task_id: str) -> Task | None:
        task = self.tasks.get(task_id)
        if task is None:
            return None
        runner = self.runners.get(task_id)
        if runner and not runner.done():
            runner.cancel()
            await asyncio.wait({runner}, timeout=10)
        return task

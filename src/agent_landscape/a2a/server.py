"""FastAPI router exposing every registered agent as an A2A JSON-RPC endpoint.

Paths intentionally match LiteLLM's Agent Gateway so agents can be registered
there 1:1 (``agent_card_params.url = http://host:port/a2a/<agent_id>``):

    POST /a2a/{agent_id}                                  JSON-RPC 2.0 (all methods)
    POST /a2a/{agent_id}/message/send                     alias for message/send
    GET  /a2a/{agent_id}/.well-known/agent-card.json      agent card
    GET  /a2a/{agent_id}/.well-known/agent.json           agent card (legacy)
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Callable, Protocol

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from .task_manager import Executor, TaskManager
from .types import (
    TERMINAL_STATES,
    AgentCard,
    MessageSendParams,
    RPCError,
    Task,
    TaskIdParams,
    TaskListParams,
    TaskQueryParams,
    TaskStatusUpdateEvent,
    rpc_error,
    rpc_result,
)


class A2AAgent(Executor, Protocol):
    id: str

    def card(self) -> AgentCard: ...


Resolver = Callable[[str], A2AAgent | None]


def _task_view(task: Task, history_length: int | None) -> dict[str, Any]:
    data = task.dump()
    if history_length is not None:
        data["history"] = data.get("history", [])[-history_length:] if history_length > 0 else []
    return data


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def create_a2a_router(resolve: Resolver, tasks: TaskManager) -> APIRouter:
    router = APIRouter(tags=["a2a"])

    def _card(agent_id: str) -> JSONResponse:
        agent = resolve(agent_id)
        if agent is None:
            return JSONResponse({"error": f"Unknown agent {agent_id}"}, status_code=404)
        return JSONResponse(agent.card().dump())

    @router.get("/a2a/{agent_id}/.well-known/agent-card.json")
    async def agent_card(agent_id: str):
        return _card(agent_id)

    @router.get("/a2a/{agent_id}/.well-known/agent.json")
    async def agent_card_legacy(agent_id: str):
        return _card(agent_id)

    @router.post("/a2a/{agent_id}/message/send")
    async def message_send_alias(agent_id: str, request: Request):
        return await _handle(agent_id, request, force_method="message/send")

    @router.post("/a2a/{agent_id}")
    async def jsonrpc(agent_id: str, request: Request):
        return await _handle(agent_id, request)

    async def _handle(agent_id: str, request: Request, force_method: str | None = None):
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse(rpc_error(None, RPCError.PARSE_ERROR, "Parse error"))
        if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or "method" not in body:
            return JSONResponse(rpc_error(body.get("id") if isinstance(body, dict) else None,
                                          RPCError.INVALID_REQUEST, "Invalid Request"))
        req_id = body.get("id")
        method = force_method or body["method"]
        params = body.get("params") or {}

        agent = resolve(agent_id)
        if agent is None:
            return JSONResponse(rpc_error(req_id, RPCError.INVALID_PARAMS, f"Unknown agent {agent_id}"),
                                status_code=404)
        headers = dict(request.headers)

        try:
            if method == "message/send":
                p = MessageSendParams.model_validate(params)
                task, runner = tasks.start(agent, p, agent_id=agent.id, headers=headers)
                blocking = True if p.configuration is None or p.configuration.blocking is None \
                    else p.configuration.blocking
                if blocking:
                    # asyncio.wait does not cancel the runner if the client goes away
                    await asyncio.wait({runner})
                hl = p.configuration.history_length if p.configuration else None
                return JSONResponse(rpc_result(req_id, _task_view(task, hl)))

            if method == "message/stream":
                p = MessageSendParams.model_validate(params)
                task, _ = tasks.start(agent, p, agent_id=agent.id, headers=headers)
                q = tasks.subscribe(task.id)
                return StreamingResponse(_stream(req_id, task, q), media_type="text/event-stream")

            if method == "tasks/resubscribe":
                p = TaskIdParams.model_validate(params)
                task = tasks.get(p.id)
                if task is None:
                    return JSONResponse(rpc_error(req_id, RPCError.TASK_NOT_FOUND, "Task not found"))
                q = tasks.subscribe(task.id)
                return StreamingResponse(_stream(req_id, task, q), media_type="text/event-stream")

            if method == "tasks/get":
                p = TaskQueryParams.model_validate(params)
                task = tasks.get(p.id)
                if task is None:
                    return JSONResponse(rpc_error(req_id, RPCError.TASK_NOT_FOUND, "Task not found"))
                return JSONResponse(rpc_result(req_id, _task_view(task, p.history_length)))

            if method == "tasks/list":
                p = TaskListParams.model_validate(params)
                items = [t.dump() for t in tasks.list(context_id=p.context_id, agent_id=agent.id)]
                return JSONResponse(rpc_result(req_id, items))

            if method == "tasks/cancel":
                p = TaskIdParams.model_validate(params)
                task = tasks.get(p.id)
                if task is None:
                    return JSONResponse(rpc_error(req_id, RPCError.TASK_NOT_FOUND, "Task not found"))
                if task.status.state in TERMINAL_STATES:
                    return JSONResponse(rpc_error(req_id, RPCError.TASK_NOT_CANCELABLE, "Task not cancelable"))
                await tasks.cancel(p.id)
                return JSONResponse(rpc_result(req_id, task.dump()))

            if method == "agent/getAuthenticatedExtendedCard":
                return JSONResponse(rpc_result(req_id, agent.card().dump()))

            if method.startswith("tasks/pushNotificationConfig/"):
                return JSONResponse(rpc_error(req_id, RPCError.UNSUPPORTED_OPERATION,
                                              "Push notifications are not supported"))

            return JSONResponse(rpc_error(req_id, RPCError.METHOD_NOT_FOUND, f"Method not found: {method}"))
        except ValidationError as exc:
            return JSONResponse(rpc_error(req_id, RPCError.INVALID_PARAMS, "Invalid params",
                                          json.loads(exc.json())))
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(rpc_error(req_id, RPCError.INTERNAL_ERROR, str(exc)))

    async def _stream(req_id: Any, task: Task, q: asyncio.Queue) -> AsyncIterator[str]:
        try:
            yield _sse(rpc_result(req_id, task.dump()))
            if task.status.state in TERMINAL_STATES:
                return
            while True:
                ev = await q.get()
                yield _sse(rpc_result(req_id, ev.dump()))
                if isinstance(ev, TaskStatusUpdateEvent) and ev.final:
                    return
        finally:
            tasks.unsubscribe(task.id, q)

    return router

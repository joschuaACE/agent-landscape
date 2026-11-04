"""Minimal async A2A JSON-RPC client (works against this server, LiteLLM's
Agent Gateway ``/a2a/{agent}`` or any spec-compliant A2A 0.3 agent)."""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from .types import AgentCard, Message, MessageSendConfiguration, StreamEvent, Task, new_id, parse_event


class A2AError(RuntimeError):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"A2A error {code}: {message}")
        self.code, self.data = code, data


class A2AClient:
    def __init__(self, url: str, http: httpx.AsyncClient, headers: dict[str, str] | None = None) -> None:
        self.url = url.rstrip("/")
        self.http = http
        self.headers = headers or {}

    async def get_card(self) -> AgentCard:
        last: Exception | None = None
        for path in ("/.well-known/agent-card.json", "/.well-known/agent.json"):
            try:
                r = await self.http.get(self.url + path, headers=self.headers)
                r.raise_for_status()
                return AgentCard.model_validate(r.json())
            except Exception as exc:  # noqa: BLE001
                last = exc
        raise RuntimeError(f"Could not fetch agent card from {self.url}: {last}")

    async def _rpc(self, method: str, params: dict[str, Any]) -> Any:
        payload = {"jsonrpc": "2.0", "id": new_id(), "method": method, "params": params}
        r = await self.http.post(self.url, json=payload, headers=self.headers)
        r.raise_for_status()
        body = r.json()
        if "error" in body and body["error"]:
            e = body["error"]
            raise A2AError(e.get("code", -1), e.get("message", "error"), e.get("data"))
        return body.get("result")

    async def send(self, message: Message, *, blocking: bool = True,
                   metadata: dict[str, Any] | None = None) -> StreamEvent:
        params: dict[str, Any] = {
            "message": message.dump(),
            "configuration": MessageSendConfiguration(blocking=blocking).dump(),
        }
        if metadata:
            params["metadata"] = metadata
        return parse_event(await self._rpc("message/send", params))

    async def stream(self, message: Message, *, metadata: dict[str, Any] | None = None
                     ) -> AsyncIterator[StreamEvent]:
        params: dict[str, Any] = {"message": message.dump()}
        if metadata:
            params["metadata"] = metadata
        payload = {"jsonrpc": "2.0", "id": new_id(), "method": "message/stream", "params": params}
        headers = {**self.headers, "Accept": "text/event-stream"}
        async with self.http.stream("POST", self.url, json=payload, headers=headers) as resp:
            resp.raise_for_status()
            buf: list[str] = []
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    buf.append(line[5:].strip())
                    continue
                if line.strip() == "" and buf:
                    yield self._unwrap("".join(buf))
                    buf = []
                elif line.strip().startswith("{"):  # NDJSON fallback
                    yield self._unwrap(line)
            if buf:
                yield self._unwrap("".join(buf))

    @staticmethod
    def _unwrap(raw: str) -> StreamEvent:
        body = json.loads(raw)
        if body.get("error"):
            e = body["error"]
            raise A2AError(e.get("code", -1), e.get("message", "error"), e.get("data"))
        return parse_event(body.get("result", body))

    async def get_task(self, task_id: str, history_length: int | None = None) -> Task:
        params: dict[str, Any] = {"id": task_id}
        if history_length is not None:
            params["historyLength"] = history_length
        return Task.model_validate(await self._rpc("tasks/get", params))

    async def cancel(self, task_id: str) -> Task:
        return Task.model_validate(await self._rpc("tasks/cancel", {"id": task_id}))

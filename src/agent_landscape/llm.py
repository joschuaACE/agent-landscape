"""Thin LiteLLM wrapper: direct provider calls, LiteLLM Proxy routing, or mock mode."""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from typing import Any

import httpx
import litellm

from .config import Settings

log = logging.getLogger(__name__)
litellm.drop_params = True  # e.g. drop `temperature` for models that reject it
litellm.suppress_debug_info = True


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class LLMResult:
    text: str
    tokens: int = 0
    cost: float = 0.0
    model: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)

    def assistant_message(self) -> dict[str, Any]:
        """The assistant turn to append before tool results (OpenAI format)."""
        return {"role": "assistant", "content": self.text or None, "tool_calls": [
            {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.arguments}}
            for tc in self.tool_calls]}


class LLM:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # models that rejected `temperature` (e.g. reasoning models behind a LiteLLM proxy)
        self._no_temperature: set[str] = set()

    def resolve_model(self, model: str | None) -> str:
        m = model or self.settings.default_model
        if self.settings.proxy_url and not self.settings.mock and not m.startswith("litellm_proxy/"):
            m = f"litellm_proxy/{m}"
        return m

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        mock: str | None = None,
        temperature: float = 0.7,
        headers: dict[str, str] | None = None,
        metadata: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResult:
        s = self.settings
        kwargs: dict[str, Any] = {
            "model": self.resolve_model(model),
            "messages": messages,
            "temperature": temperature,
        }
        if tools and not s.mock:
            kwargs["tools"] = tools
        if metadata:
            kwargs["metadata"] = metadata
        if s.mock:
            await asyncio.sleep(random.uniform(*s.mock_delay))
            kwargs["mock_response"] = mock or "Acknowledged. Task analysed and completed."
            if "/" not in kwargs["model"]:
                # proxy-only names (e.g. "bc2_claude-haiku-5-5") have no provider LiteLLM can infer;
                # mock calls never reach a provider, so any one will do
                kwargs["custom_llm_provider"] = "openai"
        else:
            if s.proxy_url:
                kwargs["api_base"] = s.proxy_url
                if s.proxy_key:
                    kwargs["api_key"] = s.proxy_key
            if headers:
                kwargs["extra_headers"] = headers
        if kwargs["model"] in self._no_temperature:
            kwargs.pop("temperature", None)

        try:
            resp = await litellm.acompletion(**kwargs)
        except Exception as exc:
            # proxies enforce params server-side, so client drop_params can't help → retry without it
            if "temperature" not in kwargs or "temperature" not in str(exc).lower():
                raise
            log.info("Model %s rejected temperature; retrying without it", kwargs["model"])
            self._no_temperature.add(kwargs["model"])
            kwargs.pop("temperature")
            resp = await litellm.acompletion(**kwargs)
        msg = resp.choices[0].message
        text = msg.content or ""
        tool_calls = [ToolCall(id=tc.id or f"call_{i}", name=tc.function.name,
                               arguments=tc.function.arguments or "{}")
                      for i, tc in enumerate(getattr(msg, "tool_calls", None) or [])]

        tokens = 0
        usage = getattr(resp, "usage", None)
        if usage is not None:
            tokens = int(getattr(usage, "total_tokens", 0) or 0)
        if s.mock:  # make the mock economy feel realistic
            tokens = sum(len(str(m.get("content") or "")) for m in messages) // 4 + len(text) // 4

        cost = 0.0
        try:
            cost = float((getattr(resp, "_hidden_params", None) or {}).get("response_cost") or 0.0)
            if not cost and not s.mock:
                cost = float(litellm.completion_cost(completion_response=resp) or 0.0)
        except Exception:  # noqa: BLE001 – unknown model pricing
            pass
        return LLMResult(text=text, tokens=tokens, cost=cost, model=kwargs["model"], tool_calls=tool_calls)

    async def proxy_models(self) -> dict[str, str] | None:
        """Public model names on the LiteLLM proxy → their underlying provider model.

        The proxy routes by the *public* ``model_name`` given when adding a model (bold in the
        LiteLLM UI). The underlying ``litellm_params.model`` shown below it is not callable.
        Returns None when no proxy is used or it can't be reached.
        """
        s = self.settings
        if not s.proxy_url or s.mock:
            return None
        headers = {"Authorization": f"Bearer {s.proxy_key}"} if s.proxy_key else {}
        try:
            async with httpx.AsyncClient(timeout=3) as http:
                r = await http.get(f"{s.proxy_url}/v1/model/info", headers=headers)
                if r.status_code == 200:
                    return {m["model_name"]: str((m.get("litellm_params") or {}).get("model") or "")
                            for m in r.json().get("data", []) if m.get("model_name")}
                r = await http.get(f"{s.proxy_url}/v1/models", headers=headers)
                r.raise_for_status()
                return {m["id"]: "" for m in r.json().get("data", [])}
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not list LiteLLM proxy models: %s", exc)
            return None

    async def list_models(self) -> list[str]:
        proxy = await self.proxy_models()
        if proxy is not None:  # only offer names the proxy can actually serve
            return sorted(proxy)
        s = self.settings
        return list(dict.fromkeys(m for m in (s.default_model, s.orchestrator_model) if m))

    async def check_models(self) -> list[str]:
        """Explain configured model names the LiteLLM proxy can't serve (empty list = all good)."""
        proxy = await self.proxy_models()
        if proxy is None:
            return []
        s = self.settings
        problems: list[str] = []
        for var, name in (("DEFAULT_MODEL", s.default_model), ("ORCHESTRATOR_MODEL", s.orchestrator_model)):
            bare = (name or "").removeprefix("litellm_proxy/")
            if not bare or bare in proxy:
                continue
            public = [p for p, under in proxy.items() if under and under.split("/")[-1] == bare.split("/")[-1]]
            if public:
                problems.append(f"{var}={bare} is the underlying model behind LiteLLM model {', '.join(public)}. "
                                f"The proxy only accepts the public name: set {var}={public[0]}")
            else:
                problems.append(f"{var}={bare} is not a model on the LiteLLM proxy. "
                                f"Available: {', '.join(sorted(proxy)) or 'none'}")
        return problems

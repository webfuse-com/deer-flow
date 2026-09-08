"""Switch routine execution turns to a no-thinking model variant."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import override

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import ToolMessage


class AdaptiveReasoningMiddleware(AgentMiddleware):
    def __init__(self, routine_model, *, routine_tools: list[str]) -> None:
        super().__init__()
        self._routine_model = routine_model
        self._routine_tools = frozenset(routine_tools)

    def _routine_turn(self, request: ModelRequest) -> bool:
        trailing: list[ToolMessage] = []
        for message in reversed(list(request.messages)):
            if not isinstance(message, ToolMessage):
                break
            trailing.append(message)
        if not trailing:
            return False
        for message in trailing:
            meta = (message.additional_kwargs or {}).get("deerflow_tool_meta")
            status = meta.get("status") if isinstance(meta, dict) else message.status
            if status != "success" or message.name not in self._routine_tools:
                return False
        return True

    def _prepare(self, request: ModelRequest) -> ModelRequest:
        routine = self._routine_turn(request)
        context = getattr(request.runtime, "context", None)
        if isinstance(context, dict):
            mode = "routine" if routine else "reasoning"
            if context.get("adaptive_reasoning_mode") != mode:
                context["adaptive_reasoning_switches"] = int(context.get("adaptive_reasoning_switches", 0)) + 1
            context["adaptive_reasoning_mode"] = mode
        return request.override(model=self._routine_model) if routine else request

    @override
    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]) -> ModelCallResult:
        return handler(self._prepare(request))

    @override
    async def awrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]) -> ModelCallResult:
        return await handler(self._prepare(request))

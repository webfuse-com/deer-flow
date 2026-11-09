"""[argus patch #174, #175] Call-time backstop for a tiered run's tool ceiling, and taint gating.

Assembly already binds only tools inside the tier's ceiling and asserts it
(``deerflow.agents.tier_policy.verify_tier_tool_view``). This middleware is the
outermost tool wrapper of a tiered run and refuses any call whose tool name is
not in the set the build bound, so a tool that reaches the tool node some other
way (a promotion path, a later addition, a hallucinated name that happens to
resolve) never executes. The refusal is an error ToolMessage, not an
exception, so the model sees it and the turn continues.

[argus patch #175] With ``taint`` (the owner tier), the run is tainted once any
of its AI messages calls a PRIVATE tool, or memory was injected into it. The
check reads the run's own messages (the worker's pre-run message ids mark the
boundary), so a PRIVATE call in the same model step as a write is seen before
either executes. A tainted run's WRITE and egress calls get
``tier_policy.TAINT_REFUSAL``, and its AI messages are stamped with the source
labels (``tier_policy.TURN_PRIVATE_KEY``) so the channel manager can flag the
answer posts.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.runtime import Runtime
from langgraph.types import Command

from deerflow.agents.tier_policy import TAINT_REFUSAL, TURN_PRIVATE_KEY, TaintClasses, current_run_messages, run_private_sources
from deerflow.runtime.context_keys import CURRENT_RUN_PRE_EXISTING_MESSAGE_IDS_KEY

logger = logging.getLogger(__name__)


def _pre_existing_ids(runtime: Any) -> object:
    context = getattr(runtime, "context", None)
    return context.get(CURRENT_RUN_PRE_EXISTING_MESSAGE_IDS_KEY) if isinstance(context, dict) else None


class TierToolGuardMiddleware(AgentMiddleware[AgentState]):
    """Refuse any tool call outside ``allowed_names``; with ``taint``, gate writes and egress."""

    def __init__(self, tier: str, allowed_names: Iterable[str], *, taint: TaintClasses | None = None) -> None:
        super().__init__()
        self.tier = tier
        self.allowed_names = frozenset(allowed_names)
        self.taint = taint

    def _run_sources(self, state: Any, runtime: Any) -> list[str]:
        if self.taint is None:
            return []
        messages = (state or {}).get("messages") if isinstance(state, dict) else getattr(state, "messages", None)
        return run_private_sources(current_run_messages(messages or [], _pre_existing_ids(runtime)), self.taint)

    def _refusal(self, request: ToolCallRequest) -> ToolMessage | None:
        call = request.tool_call or {}
        name = call.get("name")
        if not isinstance(name, str) or name not in self.allowed_names:
            logger.warning("[tier-policy] tier %s: refused a call to %r outside the tool ceiling", self.tier, name)
            return self._error(call, f"Error: the tool {name!r} is not available in this conversation.")
        if self.taint is not None and self.taint.blocked_after_taint(name):
            sources = self._run_sources(getattr(request, "state", None), getattr(request, "runtime", None))
            if sources:
                logger.info("[tier-policy] tier %s: refused %r after private reads (%s)", self.tier, name, ", ".join(sources))
                return self._error(call, TAINT_REFUSAL)
        return None

    @staticmethod
    def _error(call: dict, content: str) -> ToolMessage:
        name = call.get("name")
        return ToolMessage(content=content, tool_call_id=str(call.get("id") or ""), name=name if isinstance(name, str) else None, status="error")

    def _stamp(self, state: Any, runtime: Any) -> dict | None:
        """Stamp the newest AI message of a tainted run with its source labels."""
        if self.taint is None:
            return None
        messages = (state or {}).get("messages") if isinstance(state, dict) else None
        if not messages or not isinstance(messages[-1], AIMessage):
            return None
        sources = self._run_sources(state, runtime)
        last = messages[-1]
        if not sources or last.additional_kwargs.get(TURN_PRIVATE_KEY) == sources:
            return None
        return {"messages": [last.model_copy(update={"additional_kwargs": {**last.additional_kwargs, TURN_PRIVATE_KEY: sources}})]}

    @override
    def after_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._stamp(state, runtime)

    @override
    def after_agent(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._stamp(state, runtime)

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        refusal = self._refusal(request)
        return refusal if refusal is not None else handler(request)

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        refusal = self._refusal(request)
        return refusal if refusal is not None else await handler(request)

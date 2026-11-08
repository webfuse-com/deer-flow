"""[argus patch #174] Call-time backstop for a tiered run's tool ceiling.

Assembly already binds only tools inside the tier's ceiling and asserts it
(``deerflow.agents.tier_policy.verify_tier_tool_view``). This middleware is the
outermost tool wrapper of a tiered run and refuses any call whose tool name is
not in the set the build bound, so a tool that reaches the tool node some other
way (a promotion path, a later addition, a hallucinated name that happens to
resolve) never executes. The refusal is an error ToolMessage, not an
exception, so the model sees it and the turn continues.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

logger = logging.getLogger(__name__)


class TierToolGuardMiddleware(AgentMiddleware[AgentState]):
    """Refuse any tool call outside ``allowed_names`` for this tiered run."""

    def __init__(self, tier: str, allowed_names: Iterable[str]) -> None:
        super().__init__()
        self.tier = tier
        self.allowed_names = frozenset(allowed_names)

    def _refusal(self, request: ToolCallRequest) -> ToolMessage | None:
        call = request.tool_call or {}
        name = call.get("name")
        if isinstance(name, str) and name in self.allowed_names:
            return None
        logger.warning("[tier-policy] tier %s: refused a call to %r outside the tool ceiling", self.tier, name)
        return ToolMessage(
            content=f"Error: the tool {name!r} is not available in this conversation.",
            tool_call_id=str(call.get("id") or ""),
            name=name if isinstance(name, str) else None,
            status="error",
        )

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

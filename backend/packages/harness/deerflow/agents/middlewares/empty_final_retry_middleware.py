"""[argus patch #37] Retry once when the model returns a blank FINAL turn.

local-qwen occasionally ends a run with an empty final ``AIMessage`` — no
content and no ``tool_calls`` — typically after a long tool loop. The run still
completes "successfully", so the user gets a blank answer (silently on the web
UI; as ``(No response from agent)`` on channels via patch #36). Patch #36 and
the web guard (patch #37, pagination.py) make the blank *visible*; this
middleware attacks the root cause: detect the blank final at the model-call
boundary and re-invoke the model once before the turn is committed, so a real
answer is produced most of the time.

Scope is deliberately narrow to avoid perturbing normal runs:
  * Only a FINAL turn is retried — an ``AIMessage`` with **no** ``tool_calls``.
    A blank message that carries tool_calls is a normal intermediate step
    (the content is empty because the turn's payload is the tool call) and is
    left untouched.
  * Retry happens **at most once** per model call. If the retry is also blank,
    the blank is returned as-is and the downstream visible-marker guards handle
    it — we never loop.
  * Everything non-blank is returned unchanged; no message is rewritten here.

Placed before LoopDetectionMiddleware in the lead-agent chain so a retried
final still passes through loop detection normally.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import override

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage

from deerflow.utils.messages import is_blank_text

logger = logging.getLogger(__name__)


def _is_blank_final(response: ModelResponse) -> bool:
    """True when *response*'s last message is a blank FINAL assistant turn.

    Final = an ``AIMessage`` with no ``tool_calls``. Blank = empty /
    whitespace / short non-alphanumeric filler (:func:`is_blank_text`).
    """
    result = getattr(response, "result", None)
    if not result:
        return False
    last = result[-1]
    is_ai = isinstance(last, AIMessage) or getattr(last, "type", None) == "ai"
    if not is_ai:
        return False
    if getattr(last, "tool_calls", None):
        # Has tool calls -> a normal intermediate turn, not a final answer.
        return False
    return is_blank_text(getattr(last, "content", ""))


class EmptyFinalRetryMiddleware(AgentMiddleware):
    """Re-invoke the model once if it returns a blank final assistant turn."""

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        response = handler(request)
        if _is_blank_final(response):
            logger.warning("Empty final model turn; retrying once (sync).")
            retry = handler(request)
            if not _is_blank_final(retry):
                return retry
            logger.warning("Retry also produced an empty final turn; surfacing blank.")
            return retry
        return response

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        response = await handler(request)
        if _is_blank_final(response):
            logger.warning("Empty final model turn; retrying once (async).")
            retry = await handler(request)
            if not _is_blank_final(retry):
                return retry
            logger.warning("Retry also produced an empty final turn; surfacing blank.")
            return retry
        return response

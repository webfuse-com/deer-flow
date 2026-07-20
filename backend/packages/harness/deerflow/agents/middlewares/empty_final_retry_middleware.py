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
  * [argus patch #44] Unattended (scheduled-playbook) turns are NEVER retried.
    There, a blank final (or the ``.`` no-op sentinel the playbook prompts ask
    for) is the *desired* outcome — the channel manager's silence branch
    (patches #31/#32/#34) suppresses the send. Retrying it re-samples the
    model, which frequently narrates instead ("No meetings in the window...")
    and turns a compliant silent turn into hourly channel noise. The flag
    rides run_context -> runtime.context (set in app/channels/manager.py),
    the same signal the memory write policy uses.

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


def _is_unattended(request: ModelRequest) -> bool:
    """[argus patch #44] True when this model call belongs to an unattended
    (scheduled-playbook) run.

    The channel manager sets ``unattended=True`` on run_context, which rides
    to ``runtime.context`` (same signal ``memory/write_policy.py`` reads).
    Conservative: anything that is not a mapping with a truthy ``unattended``
    key counts as attended, so the retry behavior of interactive turns is
    unchanged even when a runtime/context is absent (as in unit tests).
    """
    runtime = getattr(request, "runtime", None)
    if runtime is None:
        return False
    ctx = getattr(runtime, "context", None)
    if not isinstance(ctx, dict):
        return False
    return bool(ctx.get("unattended"))


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
            if _is_unattended(request):
                # [argus patch #44] Silence is the desired outcome on a
                # scheduled turn; the channel manager suppresses the send.
                logger.info("Blank final on unattended turn; not retrying.")
                return response
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
            if _is_unattended(request):
                # [argus patch #44] Silence is the desired outcome on a
                # scheduled turn; the channel manager suppresses the send.
                logger.info("Blank final on unattended turn; not retrying.")
                return response
            logger.warning("Empty final model turn; retrying once (async).")
            retry = await handler(request)
            if not _is_blank_final(retry):
                return retry
            logger.warning("Retry also produced an empty final turn; surfacing blank.")
            return retry
        return response

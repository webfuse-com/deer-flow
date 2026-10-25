"""[argus patch #37] Retry once when the model ends a turn with filler only.

local-qwen occasionally ends a run with a final ``AIMessage`` that carries no
deliverable text, typically after a long tool loop. Two shapes exist:

  * truly empty or whitespace-only content. Upstream's model-boundary
    recovery (``LLMErrorHandlingMiddleware``, #5080) raises
    ``EmptyModelResponseError`` for it, retries once per run and then returns
    a visible fallback, so this middleware leaves that shape alone (retrying
    it here as well sampled one blank up to four times);
  * short non-alphanumeric filler (``.``, ``...``, ``…``), which upstream
    counts as visible text and delivers as the answer. This middleware
    re-invokes the model once for it, before the turn is committed, so a real
    answer is produced most of the time. Patch #36 and the web guard
    (pagination.py) keep a filler answer visible as a marker when the retry
    is filler again.

Scope is deliberately narrow to avoid perturbing normal runs:
  * Only a FINAL turn is retried: an ``AIMessage`` with **no** ``tool_calls``.
  * Retry happens **at most once** per model call; it never loops.
  * Everything else is returned unchanged; no message is rewritten here.
  * [argus patch #44] Unattended (scheduled-playbook) turns are NEVER retried.
    There, a blank final (or the ``.`` no-op sentinel the playbook prompts ask
    for) is the *desired* outcome: the channel manager's silence branch
    (patches #31/#32/#34) suppresses the send. Retrying it re-samples the
    model, which frequently narrates instead ("No meetings in the window...")
    and turns a compliant silent turn into hourly channel noise. The flag
    rides run_context -> runtime.context (set in app/channels/manager.py and
    the playbook web-chat route), the same signal the memory write policy
    uses. :func:`is_unattended_run` is also what exempts unattended runs from
    upstream's empty-response retry and post-tool fallback.

Placed before LoopDetectionMiddleware in the lead-agent chain so a retried
final still passes through loop detection normally.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, override

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage

from deerflow.agents.middlewares.model_response import has_visible_content
from deerflow.utils.messages import is_blank_text

logger = logging.getLogger(__name__)


def is_unattended_run(runtime: Any) -> bool:
    """[argus patch #44] True when *runtime* belongs to an unattended (scheduled) run.

    The channel manager and the playbook web-chat route set ``unattended=True``
    on the run context, which LangGraph exposes as ``runtime.context`` (the
    same signal ``memory/write_policy.py`` reads). Conservative: anything that
    is not a mapping with a truthy ``unattended`` key counts as attended, so
    interactive behaviour is unchanged when a runtime or context is absent.
    Subagent runs build a fresh context without the key.
    """
    context = getattr(runtime, "context", None)
    return isinstance(context, Mapping) and bool(context.get("unattended"))


def _is_unattended(request: ModelRequest) -> bool:
    """[argus patch #44] :func:`is_unattended_run` for a model request."""
    return is_unattended_run(getattr(request, "runtime", None))


def _is_filler_final(response: ModelResponse) -> bool:
    """True when *response*'s last message is a FINAL assistant turn of filler only.

    Final = an ``AIMessage`` with no ``tool_calls``. Filler = blank by
    :func:`is_blank_text` but visible to upstream's ``has_visible_content``
    (``.``, ``...``); empty or whitespace-only content is upstream's to retry.
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
    return is_blank_text(getattr(last, "content", "")) and has_visible_content(last)


class EmptyFinalRetryMiddleware(AgentMiddleware):
    """Re-invoke the model once if it returns a filler-only final assistant turn."""

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        response = handler(request)
        if not _is_filler_final(response):
            return response
        if _is_unattended(request):
            # [argus patch #44] Silence is the desired outcome on a
            # scheduled turn; the channel manager suppresses the send.
            logger.info("Filler final on unattended turn; not retrying.")
            return response
        logger.warning("Filler-only final model turn; retrying once (sync).")
        return handler(request)

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        response = await handler(request)
        if not _is_filler_final(response):
            return response
        if _is_unattended(request):
            # [argus patch #44] Silence is the desired outcome on a
            # scheduled turn; the channel manager suppresses the send.
            logger.info("Filler final on unattended turn; not retrying.")
            return response
        logger.warning("Filler-only final model turn; retrying once (async).")
        return await handler(request)

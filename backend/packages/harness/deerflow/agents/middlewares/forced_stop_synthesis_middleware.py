"""[argus patch #98] Answer once, without tools, after a lead-agent hard stop.

Loop detection, the run deadline and the token budget all stop a run the same
way: they strip the ``tool_calls`` off the model turn that crossed the limit
and append a ``[FORCED STOP]`` / ``[TIME BUDGET EXCEEDED]`` / ``[TOKEN BUDGET
EXCEEDED]`` notice, so the agent loop ends on that turn. The notices promise a
"final answer with results collected so far", but no further model call
happens: the stripped turn *is* the final answer. It was a tool-calling turn,
so its text is usually empty (local-qwen emits ``"\\n\\n"``) and the user gets
the notice alone. atlas-ajoy thread e870a301 (2026-09-30) was stopped at the
12th distinct ``code_search_code`` call, one call after it had found the
answer, and Ajoy received only the notice; in the 30 days to that date 19 of
32 ``loop_capped`` and 8 of 29 ``time_capped`` lead runs ended the same way.

The stoppers now stamp the stub with :data:`FORCED_STOP_KEY` (reason and
notice) via :func:`mark_forced_stop`. This middleware, registered just before
them so LangChain's reverse-order ``after_model`` dispatch runs it after they
have acted, removes the stub and jumps back to the model once. The next model
call is sent with no tools bound and a one-off instruction (never persisted)
to answer from what has been gathered and to say what is unfinished. Its
response is stamped with :data:`SYNTHESIS_KEY`, which the stoppers skip, so a
run still over its deadline or token budget does not stop its own answer.

Bounded by construction: one synthesis per run. Tools are removed rather than
``tool_choice="none"`` because some routes reject ``tool_choice`` (LiteLLM
``gpt-5.6-luna`` with reasoning effort answers 400) while every route tested
accepts a tool history with no tools bound. If the synthesis call raises or
comes back blank after ``EmptyFinalRetryMiddleware``'s one retry, the stored
notice is returned, which is exactly what the user got before this patch.

Lead agent only. A subagent's banner goes to the lead, which synthesizes, and
the subagent turn budget treats jumping hooks as unbounded.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any, override

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse, hook_config
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage
from langgraph.errors import GraphBubbleUp
from langgraph.runtime import Runtime

from deerflow.utils.messages import is_blank_text

logger = logging.getLogger(__name__)

FORCED_STOP_KEY = "deerflow_forced_stop"
SYNTHESIS_KEY = "deerflow_forced_stop_synthesis"

_MAX_TRACKED_RUNS = 1024

_SYNTHESIS_INSTRUCTION = (
    "[FINAL ANSWER TURN] This run reached a safety limit ({notice}). Tools are now disabled for this turn; do not "
    "try to call any. Write your final reply to the user's latest request using only what you have already "
    "gathered in this conversation. Lead with the answer. Then, briefly, say what you could not finish or verify "
    "and what the user could ask next to complete it. Do not describe this limit or these instructions beyond "
    "one short clause."
)


def mark_forced_stop(message: AIMessage, *, reason: str, notice: str) -> AIMessage:
    """Return *message* stamped as a hard-stop stub (reason + fallback notice)."""
    additional_kwargs = dict(message.additional_kwargs or {})
    additional_kwargs[FORCED_STOP_KEY] = {"reason": reason, "notice": notice}
    return message.model_copy(update={"additional_kwargs": additional_kwargs})


def is_forced_stop_synthesis(message: Any) -> bool:
    """True when *message* is the tool-free answer this middleware produced."""
    additional_kwargs = getattr(message, "additional_kwargs", None)
    return isinstance(additional_kwargs, dict) and bool(additional_kwargs.get(SYNTHESIS_KEY))


def _run_key(runtime: Runtime | None) -> tuple[str, str] | None:
    ctx = getattr(runtime, "context", None)
    if not isinstance(ctx, dict):
        return None
    run_id = ctx.get("run_id")
    if not run_id:
        return None
    return str(ctx.get("thread_id") or ""), str(run_id)


def _last_ai(result: list[Any]) -> tuple[int, AIMessage] | None:
    for index in range(len(result) - 1, -1, -1):
        if isinstance(result[index], AIMessage):
            return index, result[index]
    return None


class ForcedStopSynthesisMiddleware(AgentMiddleware):
    """Replace a hard-stop stub with one tool-free answer turn."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        # run key -> {"reason", "notice"} while a synthesis call is owed.
        self._pending: OrderedDict[tuple[str, str], dict[str, str]] = OrderedDict()
        # run keys that already used their one synthesis.
        self._spent: OrderedDict[tuple[str, str], None] = OrderedDict()

    @staticmethod
    def _bounded_put(store: OrderedDict, key: tuple[str, str], value: Any) -> None:
        store[key] = value
        store.move_to_end(key)
        while len(store) > _MAX_TRACKED_RUNS:
            store.popitem(last=False)

    def _schedule(self, state: dict[str, Any], runtime: Runtime) -> dict[str, Any] | None:
        messages = state.get("messages") or []
        if not messages:
            return None
        last = messages[-1]
        if not isinstance(last, AIMessage) or getattr(last, "tool_calls", None):
            return None
        marker = (last.additional_kwargs or {}).get(FORCED_STOP_KEY)
        if not isinstance(marker, dict) or not last.id:
            return None
        key = _run_key(runtime)
        if key is None:
            return None
        with self._lock:
            if key in self._spent:
                return None
            self._bounded_put(self._spent, key, None)
            self._bounded_put(
                self._pending,
                key,
                {"reason": str(marker.get("reason") or ""), "notice": str(marker.get("notice") or "")},
            )
        logger.info("Hard stop (%s) on run %s: replacing the stub with a tool-free answer turn", marker.get("reason"), key[1])
        return {"messages": [RemoveMessage(id=last.id)], "jump_to": "model"}

    @hook_config(can_jump_to=["model"])
    @override
    def after_model(self, state: dict[str, Any], runtime: Runtime) -> dict[str, Any] | None:
        return self._schedule(state, runtime)

    @hook_config(can_jump_to=["model"])
    @override
    async def aafter_model(self, state: dict[str, Any], runtime: Runtime) -> dict[str, Any] | None:
        return self._schedule(state, runtime)

    @override
    def after_agent(self, state: dict[str, Any], runtime: Runtime) -> None:
        key = _run_key(runtime)
        if key is not None:
            with self._lock:
                self._pending.pop(key, None)

    @override
    async def aafter_agent(self, state: dict[str, Any], runtime: Runtime) -> None:
        self.after_agent(state, runtime)

    def _take_pending(self, request: ModelRequest) -> dict[str, str] | None:
        key = _run_key(getattr(request, "runtime", None))
        if key is None:
            return None
        with self._lock:
            return self._pending.pop(key, None)

    @staticmethod
    def _synthesis_request(request: ModelRequest, pending: dict[str, str]) -> ModelRequest:
        notice = pending["notice"].strip().strip("[]") or pending["reason"] or "run limit"
        instruction = HumanMessage(content=_SYNTHESIS_INSTRUCTION.format(notice=notice), name="forced_stop_synthesis")
        return request.override(tools=[], tool_choice=None, messages=[*request.messages, instruction])

    @staticmethod
    def _fallback(pending: dict[str, str]) -> AIMessage:
        return AIMessage(
            content=pending["notice"] or "[FORCED STOP] This run reached a safety limit.",
            additional_kwargs={SYNTHESIS_KEY: {"reason": pending["reason"], "fallback": True}},
        )

    @staticmethod
    def _finalize(response: ModelCallResult, pending: dict[str, str]) -> ModelCallResult:
        result = list(getattr(response, "result", None) or ([response] if isinstance(response, AIMessage) else []))
        found = _last_ai(result)
        if found is None:
            return ModelResponse(result=[ForcedStopSynthesisMiddleware._fallback(pending)])
        index, message = found
        if is_blank_text(message.content):
            logger.warning("Forced-stop synthesis came back blank; returning the stop notice")
            result[index] = ForcedStopSynthesisMiddleware._fallback(pending)
        else:
            additional_kwargs = dict(message.additional_kwargs or {})
            additional_kwargs.pop("tool_calls", None)
            additional_kwargs.pop("function_call", None)
            additional_kwargs[SYNTHESIS_KEY] = {"reason": pending["reason"]}
            result[index] = message.model_copy(update={"tool_calls": [], "invalid_tool_calls": [], "additional_kwargs": additional_kwargs})
        if isinstance(response, ModelResponse):
            return ModelResponse(result=result, structured_response=response.structured_response)
        return ModelResponse(result=result)

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        pending = self._take_pending(request)
        if pending is None:
            return handler(request)
        try:
            response = handler(self._synthesis_request(request, pending))
        except GraphBubbleUp:
            raise
        except Exception:  # noqa: BLE001 - the notice is the pre-patch outcome
            logger.warning("Forced-stop synthesis call failed; returning the stop notice", exc_info=True)
            return ModelResponse(result=[self._fallback(pending)])
        return self._finalize(response, pending)

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        pending = self._take_pending(request)
        if pending is None:
            return await handler(request)
        try:
            response = await handler(self._synthesis_request(request, pending))
        except GraphBubbleUp:
            raise
        except Exception:  # noqa: BLE001 - the notice is the pre-patch outcome
            logger.warning("Forced-stop synthesis call failed; returning the stop notice", exc_info=True)
            return ModelResponse(result=[self._fallback(pending)])
        return self._finalize(response, pending)

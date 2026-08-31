"""Middleware enforcing a per-run wall-clock deadline.

Why this exists alongside the token budget and loop detection: on 2026-08-26 an
Atlas run spent 60 minutes and 36.6M tokens on a single user turn and produced
nothing. Nothing stopped it, because nothing was watching the clock.

  - ``recursion_limit`` (10000 super-steps, ~580 model turns) was 20% consumed.
  - ``TokenBudgetMiddleware`` was disabled on that stack, deliberately: it counts
    cumulative tokens, which grow ~quadratically with turns, so a cap tight
    enough to catch a runaway also truncates legitimate deep work mid-artifact.
  - ``LoopDetectionMiddleware`` is call-shaped. Layer 1 hashes tool *arguments*,
    so re-researching the same question with varied commands never repeats a
    hash; Layer 2 is a per-tool volume cap. Neither sees "still going, learning
    nothing".

Wall clock is the one dimension that maps to what a person actually cares
about, and it does not penalise a long-but-productive run any more than a
long-and-useless one -- which is the honest trade: this is a ceiling, not a
progress judgement.

Mechanism mirrors :class:`~deerflow.agents.middlewares.token_budget_middleware.TokenBudgetMiddleware`
exactly, so the two behave identically from the caller's side:

  - ``after_model`` queues a wrap-up warning once, at ``warn_at_seconds``.
  - ``wrap_model_call`` injects it as a ``HumanMessage`` at the next model call,
    which preserves ``AIMessage(tool_calls)`` -> ``ToolMessage`` pairing.
  - At ``wall_clock_seconds`` the hard stop does NOT raise. It strips
    ``tool_calls`` so the agent loop terminates naturally with a final answer
    built from work already done, and stamps ``stop_reason=time_capped`` on both
    ``consume_stop_reason`` and ``runtime.context`` (#4176).

Known limit, worth stating plainly: this only runs between model calls. A run
wedged inside one very long tool call will sail past the deadline until that
call returns. Bounding that needs a check outside the graph.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.runtime import Runtime

from deerflow.agents.middlewares._bounded_dict import BoundedDict
from deerflow.config.run_limits_config import RunLimitsConfig

logger = logging.getLogger(__name__)

_DEADLINE_WARNING_MSG = (
    "[TIME BUDGET WARNING] This run has been going for {elapsed} of its {budget} limit. "
    "Start wrapping up now: stop opening new lines of investigation, and produce a final "
    "answer from what you already have. Anything still unverified, say so plainly rather "
    "than spending the remaining time on it."
)
_DEADLINE_EXCEEDED_MSG = "[TIME BUDGET EXCEEDED] This run hit its {budget} wall-clock limit. Producing a final answer from the work completed so far. State what was finished and what was not."
_CALL_WARNING_MSG = "[MODEL CALL BUDGET WARNING] This run has used {used} of {budget} model calls. Finish the current implementation and verification path; do not open new work."
_CALL_EXCEEDED_MSG = "[MODEL CALL BUDGET EXCEEDED] This run reached {budget} model calls. Returning the completed work and an explicit checkpoint for anything left."


def _format_duration(seconds: float) -> str:
    """Render a duration the way a person would say it (52s, 4m, 1h2m)."""
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m" if secs == 0 else f"{minutes}m{secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h" if minutes == 0 else f"{hours}h{minutes}m"


class RunDeadlineMiddleware(AgentMiddleware[AgentState]):
    """Stop a run that has exceeded its wall-clock budget, gracefully."""

    def __init__(self, config: RunLimitsConfig, *, clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__()
        self._config = config
        # Injectable so tests do not have to sleep through a real deadline.
        self._clock = clock
        self._lock = threading.Lock()

        # Run start is deliberately NOT cleared by ``_clear_run_state``. A single
        # run can invoke the graph more than once (the worker's goal-continuation
        # loop), and the deadline is a property of the run, not of one
        # invocation; resetting per invocation would make it unenforceable.
        # Bounded so abandoned runs cannot leak.
        self._started_at: BoundedDict[str, float] = BoundedDict(1000)
        self._warned: BoundedDict[str, bool] = BoundedDict(1000)
        self._call_warned: BoundedDict[str, bool] = BoundedDict(1000)
        self._pending_warnings: BoundedDict[str, list[str]] = BoundedDict(1000)
        self._model_calls: BoundedDict[str, int] = BoundedDict(1000)
        # Same contract as TokenBudgetMiddleware: not cleared by after_agent so
        # the executor can consume it after the run returns.
        self._stop_reason: BoundedDict[str, str] = BoundedDict(1000)

    @classmethod
    def from_config(cls, config: RunLimitsConfig) -> RunDeadlineMiddleware:
        return cls(config=config)

    def reset(self) -> None:
        with self._lock:
            self._started_at.clear()
            self._warned.clear()
            self._call_warned.clear()
            self._pending_warnings.clear()
            self._model_calls.clear()
            self._stop_reason.clear()

    def consume_stop_reason(self, run_id: str | None) -> str | None:
        """Pop and return ``"time_capped"`` if the deadline fired for this run."""
        with self._lock:
            return self._stop_reason.pop(run_id, None)

    @staticmethod
    def _get_run_id(runtime: Runtime) -> str:
        ctx = getattr(runtime, "context", None)
        if isinstance(ctx, dict) and "run_id" in ctx:
            return ctx["run_id"]
        # Fallback to runtime object ID to prevent collisions across embedded client runs
        return str(id(runtime))

    def _elapsed(self, run_id: str) -> float:
        """Seconds since this run first reached the middleware.

        ``setdefault`` is what makes the deadline span goal continuations: the
        first invocation stamps the clock and later ones read it back.
        """
        now = self._clock()
        started = self._started_at.setdefault(run_id, now)
        return max(0.0, now - started)

    @override
    def before_agent(self, state: AgentState, runtime: Runtime) -> None:
        if not self._config.enabled:
            return
        # Stamp the start even if this invocation never reaches after_model.
        with self._lock:
            self._started_at.setdefault(self._get_run_id(runtime), self._clock())

    @override
    async def abefore_agent(self, state: AgentState, runtime: Runtime) -> None:
        self.before_agent(state, runtime)

    @override
    def after_agent(self, state: AgentState, runtime: Runtime) -> None:
        if not self._config.enabled:
            return
        run_id = self._get_run_id(runtime)
        with self._lock:
            self._warned.pop(run_id, None)
            self._pending_warnings.pop(run_id, None)
            # Model calls, like elapsed time, span the worker's goal-continuation
            # invocations. They remain bounded by BoundedDict and are reset only
            # explicitly or by eviction.

    @override
    async def aafter_agent(self, state: AgentState, runtime: Runtime) -> None:
        self.after_agent(state, runtime)

    @staticmethod
    def _append_text(content: str | list[dict | None] | None, stop_msg: str) -> str | list[dict | str]:
        """Append a stop message to an AIMessage.content field."""
        if content is None:
            return stop_msg
        if isinstance(content, str):
            if content:
                return f"{content}\n\n{stop_msg}"
            return f"\n\n{stop_msg}"
        if isinstance(content, list):
            new_content = list(content)
            new_content.append({"type": "text", "text": f"\n\n{stop_msg}"})
            return new_content
        return f"{content}\n\n{stop_msg}"

    def _build_hard_stop_update(self, msg: AIMessage, stop_msg: str) -> dict[str, Any]:
        """Strip tool calls so the agent loop terminates with a final answer."""
        updated_content = self._append_text(msg.content, stop_msg)
        kwargs = dict(msg.additional_kwargs) if msg.additional_kwargs else {}
        kwargs.pop("tool_calls", None)
        kwargs.pop("function_call", None)

        response_metadata = dict(getattr(msg, "response_metadata", {}) or {})
        if response_metadata.get("finish_reason") == "tool_calls":
            response_metadata["finish_reason"] = "stop"

        stopped_msg = msg.model_copy(
            update={
                "content": updated_content,
                "tool_calls": [],
                "additional_kwargs": kwargs,
                "response_metadata": response_metadata,
            }
        )
        return {"messages": [stopped_msg]}

    def _apply(self, state: AgentState, runtime: Runtime) -> dict | None:
        if not self._config.enabled:
            return None

        messages = state.get("messages", [])
        if not messages:
            return None

        last_msg = messages[-1]
        if not isinstance(last_msg, AIMessage):
            return None

        run_id = self._get_run_id(runtime)
        budget = self._config.wall_clock_seconds

        with self._lock:
            elapsed = self._elapsed(run_id)
            calls = self._model_calls.get(run_id, 0)

            if self._config.max_model_calls and calls >= self._config.max_model_calls:
                logger.warning("Run model-call hard stop for run %s: %s calls", run_id, calls)
                self._stop_reason[run_id] = "model_calls_capped"
                ctx = getattr(runtime, "context", None)
                if isinstance(ctx, dict):
                    ctx["stop_reason"] = "model_calls_capped"
                return self._build_hard_stop_update(last_msg, _CALL_EXCEEDED_MSG.format(budget=self._config.max_model_calls))

            if elapsed >= budget:
                logger.warning(
                    "Run deadline hard stop for run %s: %.0fs elapsed of %ss budget",
                    run_id,
                    elapsed,
                    budget,
                )
                self._stop_reason[run_id] = "time_capped"
                # Also write to runtime.context so the lead worker can read it
                # without a reference to this middleware instance (#4176).
                ctx = getattr(runtime, "context", None)
                if isinstance(ctx, dict):
                    ctx["stop_reason"] = "time_capped"
                stop_text = _DEADLINE_EXCEEDED_MSG.format(budget=_format_duration(budget))
                return self._build_hard_stop_update(last_msg, stop_text)

            if elapsed >= self._config.warn_at_seconds and not self._warned.get(run_id, False):
                self._warned[run_id] = True
                logger.info(
                    "Run deadline warning for run %s: %.0fs elapsed of %ss budget",
                    run_id,
                    elapsed,
                    budget,
                )
                warn_text = _DEADLINE_WARNING_MSG.format(
                    elapsed=_format_duration(elapsed),
                    budget=_format_duration(budget),
                )
                self._pending_warnings.setdefault(run_id, []).append(warn_text)
                return None

            if self._config.warn_at_model_calls and calls >= self._config.warn_at_model_calls and not self._call_warned.get(run_id, False):
                self._call_warned[run_id] = True
                self._pending_warnings.setdefault(run_id, []).append(
                    _CALL_WARNING_MSG.format(used=calls, budget=self._config.max_model_calls)
                )

            return None

    @override
    def after_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._apply(state, runtime)

    @override
    async def aafter_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._apply(state, runtime)

    def _drain_pending_warnings(self, runtime: Runtime) -> list[str]:
        if not self._config.enabled:
            return []
        run_id = self._get_run_id(runtime)
        with self._lock:
            warnings = self._pending_warnings.pop(run_id, None)
        return warnings or []

    @staticmethod
    def _inject_warnings(request: ModelRequest, warnings: list[str]) -> ModelRequest:
        if not warnings:
            return request
        warning_msg = HumanMessage(content="\n\n".join(warnings), name="deadline_warning")
        new_messages = list(getattr(request, "messages", [])) + [warning_msg]
        return request.override(messages=new_messages)

    @override
    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]) -> ModelCallResult:
        if self._config.enabled:
            run_id = self._get_run_id(request.runtime)
            with self._lock:
                self._model_calls[run_id] = self._model_calls.get(run_id, 0) + 1
        warnings = self._drain_pending_warnings(request.runtime)
        return handler(self._inject_warnings(request, warnings))

    @override
    async def awrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]) -> ModelCallResult:
        if self._config.enabled:
            run_id = self._get_run_id(request.runtime)
            with self._lock:
                self._model_calls[run_id] = self._model_calls.get(run_id, 0) + 1
        warnings = self._drain_pending_warnings(request.runtime)
        return await handler(self._inject_warnings(request, warnings))

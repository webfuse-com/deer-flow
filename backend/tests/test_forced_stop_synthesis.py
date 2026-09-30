"""[argus patch #98] A lead-agent hard stop ends with a tool-free answer turn.

Graph tests build a real ``langchain.agents.create_agent`` graph so the
reverse-order ``after_model`` dispatch, the ``RemoveMessage`` of the stub and
the ``jump_to: model`` re-entry are exercised the way the gateway runs them.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from deerflow.agents.middlewares.empty_final_retry_middleware import EmptyFinalRetryMiddleware
from deerflow.agents.middlewares.forced_stop_synthesis_middleware import (
    FORCED_STOP_KEY,
    SYNTHESIS_KEY,
    ForcedStopSynthesisMiddleware,
    is_forced_stop_synthesis,
    mark_forced_stop,
)
from deerflow.agents.middlewares.loop_detection_middleware import LoopDetectionMiddleware
from deerflow.agents.middlewares.run_deadline_middleware import RunDeadlineMiddleware
from deerflow.agents.middlewares.todo_middleware import TodoMiddleware
from deerflow.agents.middlewares.token_budget_middleware import TokenBudgetMiddleware
from deerflow.config.run_limits_config import RunLimitsConfig
from deerflow.config.token_budget_config import TokenBudgetConfig

_ANSWER = "The consent screen is the extension permission popup."


@tool
def code_search(query: str) -> str:
    """Pretend to search code."""
    return f"result for {query}: " + " ".join(f"word{query}{i}" for i in range(12))


class _SearchThenAnswerModel(BaseChatModel):
    """Calls ``code_search`` with a fresh query while tools are bound; answers once they are not."""

    calls: list[dict] = []
    synthesis: str | Exception = _ANSWER

    @property
    def _llm_type(self) -> str:
        return "fake-search-then-answer"

    def bind_tools(self, tools, **kwargs):
        return self.model_copy(update={"bound": list(tools)})

    bound: list = []

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append({"tools": [getattr(t, "name", t) for t in self.bound], "messages": list(messages)})
        if not self.bound:
            if isinstance(self.synthesis, Exception):
                raise self.synthesis
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.synthesis))])
        n = len(self.calls)
        message = AIMessage(
            content="\n\n",
            tool_calls=[{"id": f"call_{n}", "name": "code_search", "args": {"query": f"q{n}"}}],
        )
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def _loop_detection() -> LoopDetectionMiddleware:
    return LoopDetectionMiddleware(tool_freq_overrides={"code_search": (2, 3)})


def _graph(model, *, synthesis: bool = True, state_schema=None):
    middleware = []
    if synthesis:
        middleware.append(ForcedStopSynthesisMiddleware())
    middleware += [EmptyFinalRetryMiddleware(), _loop_detection()]
    kwargs = {"state_schema": state_schema} if state_schema is not None else {}
    return create_agent(model=model, tools=[code_search], middleware=middleware, **kwargs)


def _run(graph, *, context: dict | None = None):
    context = {"thread_id": "t1", "run_id": f"r-{time.monotonic_ns()}"} if context is None else context
    return graph.invoke({"messages": [HumanMessage(content="what is the consent screen?")]}, context=context)


def _model(synthesis: str | Exception = _ANSWER) -> _SearchThenAnswerModel:
    return _SearchThenAnswerModel(calls=[], synthesis=synthesis)


def test_hard_stop_is_replaced_by_a_tool_free_answer():
    model = _model()
    result = _run(_graph(model))

    final = result["messages"][-1]
    assert isinstance(final, AIMessage)
    assert final.content == _ANSWER
    assert is_forced_stop_synthesis(final)
    assert final.additional_kwargs[SYNTHESIS_KEY]["reason"] == "loop_capped"
    # The stub (and its notice) is gone from the thread.
    assert not any(FORCED_STOP_KEY in (getattr(m, "additional_kwargs", None) or {}) for m in result["messages"])
    assert not any("[FORCED STOP]" in str(m.content) for m in result["messages"])
    # The last call had no tools bound, and carried the one-off instruction.
    last_call = model.calls[-1]
    assert last_call["tools"] == []
    assert "[FINAL ANSWER TURN]" in str(last_call["messages"][-1].content)
    # The instruction is never persisted.
    assert not any("[FINAL ANSWER TURN]" in str(m.content) for m in result["messages"])
    # Every tool call still has its result (stripped stub removed, not dangling).
    assert result["messages"][-2].type == "tool"


def test_thread_state_reducer_removes_the_stub():
    from deerflow.agents.thread_state import ThreadState

    result = _run(_graph(_model(), state_schema=ThreadState))

    assert result["messages"][-1].content == _ANSWER
    assert not any("[FORCED STOP]" in str(m.content) for m in result["messages"])


def test_synthesis_happens_once_per_run():
    model = _model()
    _run(_graph(model))
    assert sum(1 for c in model.calls if not c["tools"]) == 1


def test_failed_synthesis_call_falls_back_to_the_notice():
    result = _run(_graph(_model(synthesis=RuntimeError("provider down"))))

    final = result["messages"][-1]
    assert "[FORCED STOP]" in final.content
    assert final.additional_kwargs[SYNTHESIS_KEY]["fallback"] is True


def test_blank_synthesis_after_retry_falls_back_to_the_notice():
    model = _model(synthesis="\n\n")
    result = _run(_graph(model))

    final = result["messages"][-1]
    assert "[FORCED STOP]" in final.content
    # EmptyFinalRetryMiddleware retried the blank synthesis once, tools still off.
    assert [c["tools"] for c in model.calls[-2:]] == [[], []]


def test_without_run_id_the_notice_is_kept():
    result = _run(_graph(_model()), context={})

    final = result["messages"][-1]
    assert "[FORCED STOP]" in final.content
    assert FORCED_STOP_KEY in final.additional_kwargs


def test_without_the_middleware_the_stub_is_stamped_and_final():
    result = _run(_graph(_model(), synthesis=False))

    final = result["messages"][-1]
    assert final.additional_kwargs[FORCED_STOP_KEY]["reason"] == "loop_capped"
    assert "[FORCED STOP]" in final.additional_kwargs[FORCED_STOP_KEY]["notice"]


def _runtime(run_id: str = "r1") -> SimpleNamespace:
    return SimpleNamespace(context={"thread_id": "t1", "run_id": run_id})


def test_token_budget_does_not_stop_the_synthesis_answer():
    mw = TokenBudgetMiddleware.from_config(TokenBudgetConfig(enabled=True, max_tokens=1000, warn_threshold=0.5, hard_stop_threshold=0.9))
    over = AIMessage(id="a1", content="", tool_calls=[{"id": "c1", "name": "x", "args": {}}], usage_metadata={"input_tokens": 5000, "output_tokens": 10, "total_tokens": 5010})
    stopped = mw._apply({"messages": [HumanMessage(content="hi"), over]}, _runtime())
    stub = stopped["messages"][0]
    assert stub.additional_kwargs[FORCED_STOP_KEY]["reason"] == "token_capped"

    answer = AIMessage(id="a2", content=_ANSWER, additional_kwargs={SYNTHESIS_KEY: {"reason": "token_capped"}}, usage_metadata={"input_tokens": 6000, "output_tokens": 50, "total_tokens": 6050})
    assert mw._apply({"messages": [HumanMessage(content="hi"), answer]}, _runtime()) is None


def test_run_deadline_does_not_stop_the_synthesis_answer():
    clock = [0.0]
    mw = RunDeadlineMiddleware(RunLimitsConfig(enabled=True, wall_clock_seconds=60, warn_at_seconds=30), clock=lambda: clock[0])
    runtime = _runtime()
    mw.before_agent({"messages": []}, runtime)
    clock[0] = 120.0
    over = AIMessage(id="a1", content="", tool_calls=[{"id": "c1", "name": "x", "args": {}}])
    stub = mw._apply({"messages": [over]}, runtime)["messages"][0]
    assert stub.additional_kwargs[FORCED_STOP_KEY]["reason"] == "time_capped"

    answer = AIMessage(id="a2", content=_ANSWER, additional_kwargs={SYNTHESIS_KEY: {"reason": "time_capped"}})
    assert mw._apply({"messages": [answer]}, runtime) is None


@pytest.mark.parametrize("key", [FORCED_STOP_KEY, SYNTHESIS_KEY])
def test_todo_reminder_does_not_reopen_a_stopped_run(key):
    mw = TodoMiddleware()
    last = AIMessage(content="done", additional_kwargs={key: {"reason": "loop_capped"}})
    state = {"messages": [HumanMessage(content="hi"), last], "todos": [{"content": "step", "status": "in_progress"}]}
    assert mw.after_model(state, _runtime()) is None


def test_mark_forced_stop_keeps_existing_kwargs():
    message = AIMessage(content="x", additional_kwargs={"keep": 1})
    marked = mark_forced_stop(message, reason="loop_capped", notice="[FORCED STOP] n")
    assert marked.additional_kwargs == {"keep": 1, FORCED_STOP_KEY: {"reason": "loop_capped", "notice": "[FORCED STOP] n"}}
    assert message.additional_kwargs == {"keep": 1}


def test_tool_messages_are_not_scheduled():
    mw = ForcedStopSynthesisMiddleware()
    state = {"messages": [ToolMessage(content="r", tool_call_id="c1")]}
    assert mw.after_model(state, _runtime()) is None

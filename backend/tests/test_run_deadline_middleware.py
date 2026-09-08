"""Tests for RunDeadlineMiddleware (per-run wall-clock ceiling).

Mirrors tests/test_token_budget_middleware.py, since the middleware is
deliberately the same shape on a different axis. The clock is injected so the
suite never sleeps through a real deadline.
"""

from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from deerflow.agents.middlewares.run_deadline_middleware import (
    RunDeadlineMiddleware,
    _format_duration,
)
from deerflow.config.run_limits_config import RunLimitsConfig


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def make_middleware(clock: FakeClock, **overrides) -> RunDeadlineMiddleware:
    cfg = RunLimitsConfig(**{"enabled": True, "wall_clock_seconds": 100, "warn_at_seconds": 60, **overrides})
    return RunDeadlineMiddleware(cfg, clock=clock)


def make_runtime(run_id: str = "run-1") -> MagicMock:
    runtime = MagicMock()
    runtime.context = {"run_id": run_id}
    return runtime


def state_with_tool_call(content: str = "working") -> dict:
    msg = AIMessage(
        content=content,
        id="ai-1",
        tool_calls=[{"name": "bash", "args": {"command": "ls"}, "id": "tc-1"}],
    )
    msg.response_metadata = {"finish_reason": "tool_calls"}
    return {"messages": [HumanMessage(content="go"), msg]}


class TestBeforeDeadline:
    def test_no_action_before_warn_threshold(self, clock):
        mw = make_middleware(clock)
        rt = make_runtime()
        mw.before_agent({}, rt)
        clock.advance(30)
        assert mw.after_model(state_with_tool_call(), rt) is None
        assert mw._drain_pending_warnings(rt) == []

    def test_disabled_middleware_never_acts(self, clock):
        mw = make_middleware(clock, enabled=False)
        rt = make_runtime()
        mw.before_agent({}, rt)
        clock.advance(10_000)
        assert mw.after_model(state_with_tool_call(), rt) is None
        assert mw.consume_stop_reason("run-1") is None


class TestWarning:
    def test_warning_queued_at_threshold_without_mutating_state(self, clock):
        mw = make_middleware(clock)
        rt = make_runtime()
        mw.before_agent({}, rt)
        clock.advance(60)
        # Returns None: the warning must not mutate state, or it would break
        # AIMessage(tool_calls) -> ToolMessage pairing.
        assert mw.after_model(state_with_tool_call(), rt) is None
        warnings = mw._drain_pending_warnings(rt)
        assert len(warnings) == 1
        assert "TIME BUDGET WARNING" in warnings[0]

    def test_warning_injected_at_next_model_call(self, clock):
        mw = make_middleware(clock)
        rt = make_runtime()
        mw.before_agent({}, rt)
        clock.advance(60)
        mw.after_model(state_with_tool_call(), rt)

        request = MagicMock()
        request.runtime = rt
        request.messages = [HumanMessage(content="go")]
        captured = {}

        def handler(req):
            captured["messages"] = req.messages
            return "response"

        request.override.side_effect = lambda **kw: MagicMock(messages=kw["messages"], runtime=rt)
        mw.wrap_model_call(request, handler)
        injected = captured["messages"][-1]
        assert isinstance(injected, HumanMessage)
        assert injected.name == "deadline_warning"
        assert "TIME BUDGET WARNING" in injected.content

    def test_warn_only_once_per_run(self, clock):
        mw = make_middleware(clock)
        rt = make_runtime()
        mw.before_agent({}, rt)
        clock.advance(60)
        mw.after_model(state_with_tool_call(), rt)
        assert len(mw._drain_pending_warnings(rt)) == 1
        clock.advance(5)
        mw.after_model(state_with_tool_call(), rt)
        assert mw._drain_pending_warnings(rt) == []

    def test_model_call_warning_is_independent_of_time_warning(self, clock):
        mw = make_middleware(clock, max_model_calls=4, warn_at_model_calls=2)
        rt = make_runtime()
        request = MagicMock(runtime=rt, messages=[HumanMessage(content="go")])
        request.override.side_effect = lambda **kw: MagicMock(messages=kw["messages"], runtime=rt)
        mw.wrap_model_call(request, lambda _req: "ok")
        mw.wrap_model_call(request, lambda _req: "ok")
        mw.after_model(state_with_tool_call(), rt)
        warnings = mw._drain_pending_warnings(rt)
        assert len(warnings) == 1
        assert "MODEL CALL BUDGET WARNING" in warnings[0]


class TestHardStop:
    def test_hard_stop_strips_tool_calls(self, clock):
        mw = make_middleware(clock)
        rt = make_runtime()
        mw.before_agent({}, rt)
        clock.advance(100)
        update = mw.after_model(state_with_tool_call(), rt)
        assert update is not None
        stopped = update["messages"][0]
        assert stopped.tool_calls == []
        assert stopped.response_metadata["finish_reason"] == "stop"
        assert "TIME BUDGET EXCEEDED" in stopped.content
        # Original work is preserved, not replaced.
        assert "working" in stopped.content

    def test_hard_stop_stamps_time_capped_and_is_consumed_once(self, clock):
        mw = make_middleware(clock)
        rt = make_runtime()
        mw.before_agent({}, rt)
        clock.advance(100)
        mw.after_model(state_with_tool_call(), rt)
        assert rt.context["stop_reason"] == "time_capped"
        assert mw.consume_stop_reason("run-1") == "time_capped"
        assert mw.consume_stop_reason("run-1") is None

    def test_hard_stop_does_not_raise(self, clock):
        """The stop must terminate the loop gracefully, never kill the run."""
        mw = make_middleware(clock)
        rt = make_runtime()
        mw.before_agent({}, rt)
        clock.advance(10_000)
        update = mw.after_model(state_with_tool_call(), rt)
        assert isinstance(update, dict)

    def test_non_ai_last_message_is_ignored(self, clock):
        mw = make_middleware(clock)
        rt = make_runtime()
        mw.before_agent({}, rt)
        clock.advance(10_000)
        assert mw.after_model({"messages": [HumanMessage(content="go")]}, rt) is None

    def test_model_call_cap_strips_tool_calls_and_stamps_reason(self, clock):
        mw = make_middleware(clock, max_model_calls=2, warn_at_model_calls=1)
        rt = make_runtime()
        request = MagicMock(runtime=rt, messages=[HumanMessage(content="go")])
        mw.wrap_model_call(request, lambda _req: "ok")
        mw.wrap_model_call(request, lambda _req: "ok")

        update = mw.after_model(state_with_tool_call(), rt)

        assert update["messages"][0].tool_calls == []
        assert "MODEL CALL BUDGET EXCEEDED" in update["messages"][0].content
        assert rt.context["stop_reason"] == "model_calls_capped"


class TestRunScoping:
    def test_deadline_survives_after_agent_so_continuations_cannot_reset_it(self, clock):
        """The worker can invoke the graph repeatedly within one run.

        If after_agent cleared the start time, each goal continuation would
        restart the clock and the deadline could never be reached.
        """
        mw = make_middleware(clock)
        rt = make_runtime()
        mw.before_agent({}, rt)
        clock.advance(80)
        mw.after_agent({}, rt)  # end of the first graph invocation
        mw.before_agent({}, rt)  # continuation re-enters
        clock.advance(25)  # 105s total, past the 100s budget
        update = mw.after_model(state_with_tool_call(), rt)
        assert update is not None, "deadline was reset by the continuation"
        assert mw.consume_stop_reason("run-1") == "time_capped"

    def test_separate_runs_have_independent_deadlines(self, clock):
        mw = make_middleware(clock)
        rt_a, rt_b = make_runtime("run-a"), make_runtime("run-b")
        mw.before_agent({}, rt_a)
        clock.advance(100)
        mw.before_agent({}, rt_b)  # run-b starts fresh at t=1100
        assert mw.after_model(state_with_tool_call(), rt_a) is not None
        assert mw.after_model(state_with_tool_call(), rt_b) is None

    def test_model_call_count_survives_goal_continuation(self, clock):
        mw = make_middleware(clock, max_model_calls=2, warn_at_model_calls=1)
        rt = make_runtime()
        request = MagicMock(runtime=rt, messages=[HumanMessage(content="go")])
        mw.wrap_model_call(request, lambda _req: "ok")
        mw.after_agent({}, rt)
        mw.wrap_model_call(request, lambda _req: "ok")

        assert mw.after_model(state_with_tool_call(), rt) is not None


class TestConfig:
    def test_warn_at_must_precede_hard_stop(self):
        with pytest.raises(ValueError, match="must be less than"):
            RunLimitsConfig(wall_clock_seconds=100, warn_at_seconds=100)

    def test_disabled_by_default(self):
        assert RunLimitsConfig().enabled is False

    def test_model_call_warning_must_precede_cap(self):
        with pytest.raises(ValueError, match="warn_at_model_calls"):
            RunLimitsConfig(max_model_calls=10, warn_at_model_calls=10)

    def test_model_call_warning_requires_cap(self):
        with pytest.raises(ValueError, match="requires max_model_calls"):
            RunLimitsConfig(max_model_calls=0, warn_at_model_calls=10)

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [(45, "45s"), (60, "1m"), (90, "1m30s"), (1800, "30m"), (3600, "1h"), (3720, "1h2m")],
    )
    def test_duration_formatting(self, seconds, expected):
        assert _format_duration(seconds) == expected

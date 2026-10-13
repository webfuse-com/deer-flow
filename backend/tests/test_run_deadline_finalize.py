"""[argus patch #127] Finish on deadline: one batch of finishing calls survives the hard stop."""

from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from deerflow.agents.middlewares.forced_stop_synthesis_middleware import FORCED_STOP_KEY
from deerflow.agents.middlewares.run_deadline_middleware import RunDeadlineMiddleware
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


def make_mw(clock: FakeClock, **overrides) -> RunDeadlineMiddleware:
    cfg = RunLimitsConfig(**{"enabled": True, "wall_clock_seconds": 300, "warn_at_seconds": 100, "finalize_on_stop": True, **overrides})
    return RunDeadlineMiddleware(cfg, clock=clock)


def runtime(run_id: str = "run-1") -> MagicMock:
    rt = MagicMock()
    rt.context = {"run_id": run_id}
    return rt


def ai_with(*names: str, msg_id: str = "ai-1") -> dict:
    msg = AIMessage(content="", id=msg_id, tool_calls=[{"name": n, "args": {}, "id": f"tc-{i}"} for i, n in enumerate(names)])
    return {"messages": [HumanMessage(content="go"), msg]}


def request_with_tools(rt, *names: str) -> MagicMock:
    req = MagicMock()
    req.runtime = rt
    req.tools = [{"name": n} for n in names]
    req.messages = [HumanMessage(content="go")]
    req.override = lambda **kw: kw
    return req


def test_off_by_default_strips_finalize_calls(clock):
    mw = RunDeadlineMiddleware(RunLimitsConfig(enabled=True, wall_clock_seconds=300, warn_at_seconds=100), clock=clock)
    rt = runtime()
    mw.before_agent({}, rt)
    clock.advance(400)
    update = mw.after_model(ai_with("atlas_create_schedule"), rt)
    assert update is not None
    assert update["messages"][0].tool_calls == []


def test_finalize_batch_runs_once_then_stops(clock):
    mw = make_mw(clock)
    rt = runtime()
    mw.before_agent({}, rt)
    clock.advance(400)
    # the schedule calls the model emitted at the deadline are let through
    assert mw.after_model(ai_with("atlas_create_schedule", "atlas_create_schedule"), rt) is None
    assert mw.consume_stop_reason("run-1") is None
    # the next message trying anything again is stopped, grace is spent
    update = mw.after_model(ai_with("atlas_commit_app", msg_id="ai-2"), rt)
    assert update is not None and update["messages"][0].tool_calls == []
    assert FORCED_STOP_KEY in update["messages"][0].additional_kwargs
    assert mw.consume_stop_reason("run-1") == "time_capped"


def test_answer_after_grace_ends_without_stub(clock):
    mw = make_mw(clock)
    rt = runtime()
    mw.before_agent({}, rt)
    clock.advance(400)
    assert mw.after_model(ai_with("present_files"), rt) is None
    final = {"messages": [HumanMessage(content="go"), AIMessage(content="Done: app committed, schedule created.", id="ai-3")]}
    assert mw.after_model(final, rt) is None
    assert mw.consume_stop_reason("run-1") == "time_capped"
    assert rt.context["stop_reason"] == "time_capped"


def test_mixed_batch_is_stripped(clock):
    mw = make_mw(clock)
    rt = runtime()
    mw.before_agent({}, rt)
    clock.advance(400)
    update = mw.after_model(ai_with("atlas_commit_app", "bash"), rt)
    assert update is not None and update["messages"][0].tool_calls == []


def test_grace_after_model_call_cap(clock):
    mw = make_mw(clock, max_model_calls=3)
    rt = runtime()
    mw.before_agent({}, rt)
    for _ in range(3):
        mw.wrap_model_call(request_with_tools(rt), lambda r: None)
    assert mw.after_model(ai_with("atlas_commit_project"), rt) is None
    update = mw.after_model(ai_with("bash", msg_id="ai-2"), rt)
    assert update is not None
    assert mw.consume_stop_reason("run-1") == "model_calls_capped"


def test_grace_message_injected_next_call(clock):
    mw = make_mw(clock)
    rt = runtime()
    mw.before_agent({}, rt)
    clock.advance(400)
    mw.after_model(ai_with("atlas_commit_app"), rt)
    warnings = mw._drain_pending_warnings(rt)
    assert any("finishing calls" in w for w in warnings)


def test_build_runs_get_commit_wording_and_a_second_warning(clock):
    mw = make_mw(clock)
    rt = runtime()
    mw.before_agent({}, rt)
    mw.wrap_model_call(request_with_tools(rt, "bash", "atlas_commit_app"), lambda r: None)
    clock.advance(110)
    assert mw.after_model(ai_with("bash"), rt) is None
    first = mw._drain_pending_warnings(rt)
    assert len(first) == 1 and "commit it" in first[0] and "atlas_commit_app" in first[0]
    clock.advance(100)  # 210 s, before the 300 - 120 = 180 s mark? no: past it
    assert mw.after_model(ai_with("bash", msg_id="ai-2"), rt) is None
    second = mw._drain_pending_warnings(rt)
    assert len(second) == 1 and "uncommitted or unscheduled" in second[0]
    clock.advance(10)
    assert mw.after_model(ai_with("bash", msg_id="ai-3"), rt) is None
    assert mw._drain_pending_warnings(rt) == []


def test_chat_runs_keep_upstream_wording(clock):
    mw = make_mw(clock)
    rt = runtime()
    mw.before_agent({}, rt)
    mw.wrap_model_call(request_with_tools(rt, "bash", "web_search"), lambda r: None)
    clock.advance(110)
    mw.after_model(ai_with("bash"), rt)
    (warning,) = mw._drain_pending_warnings(rt)
    assert "produce a final" in warning
    clock.advance(100)
    mw.after_model(ai_with("bash", msg_id="ai-2"), rt)
    assert mw._drain_pending_warnings(rt) == []

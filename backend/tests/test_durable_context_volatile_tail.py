"""[argus patch #125] The parts of durable context that change during a run go last.

The delegation ledger, active skills, artifact handles and task notes change on
most steps of a run. Rendered right after the system prompt, each change made
SGLang's prefix cache recompute the whole conversation (40-75k tokens per call
on atlas-nicholas, 2026-10-12). With ``prompt_cache.durable_context_volatile_at_tail``
they render in a second hidden data message at the end of the request; the goal
and summary stay at the head.
"""

from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from deerflow.agents.middlewares.durable_context_middleware import _DURABLE_CONTEXT_DATA_KEY, DurableContextMiddleware

STATE = {
    "summary_text": "Earlier: built the runbook.",
    "goal": {"status": "active", "objective": "Ship v2.0.2"},
    "tool_artifacts": [{"handle": "art_1", "display_name": "report.html", "artifact_type": "file", "status": "available"}],
}


def _inject(middleware, messages, state=STATE):
    return middleware._inject(SimpleNamespace(messages=messages, state=state, override=lambda **kwargs: SimpleNamespace(**kwargs))).messages


def _data(messages):
    return [(i, m.content) for i, m in enumerate(messages) if isinstance(m, HumanMessage) and m.additional_kwargs.get(_DURABLE_CONTEXT_DATA_KEY)]


def test_default_keeps_one_block_after_the_system_prompt():
    messages = _inject(DurableContextMiddleware(), [SystemMessage(content="sys"), HumanMessage(content="go")])
    data = _data(messages)
    assert len(data) == 1 and data[0][0] == 2
    assert "Ship v2.0.2" in data[0][1] and "art_1" in data[0][1]


def test_tail_mode_keeps_goal_and_summary_at_the_head_and_the_rest_last():
    history = [SystemMessage(content="sys"), HumanMessage(content="go"), AIMessage(content="", tool_calls=[{"name": "bash", "args": {}, "id": "c1", "type": "tool_call"}]), ToolMessage(content="ok", tool_call_id="c1")]
    messages = _inject(DurableContextMiddleware(volatile_at_tail=True), history)
    (head_index, head), (tail_index, tail) = _data(messages)
    assert head_index == 2 and "Ship v2.0.2" in head and "Earlier: built the runbook." in head and "art_1" not in head
    assert tail_index == len(messages) - 1 and "art_1" in tail and "Ship v2.0.2" not in tail


def test_tail_mode_keeps_the_users_request_last():
    messages = _inject(DurableContextMiddleware(volatile_at_tail=True), [SystemMessage(content="sys"), AIMessage(content="hi"), HumanMessage(content="go")])
    assert messages[-1].content == "go"
    assert "art_1" in messages[-2].content


def test_tail_mode_head_prefix_is_identical_across_steps():
    """Only the tail moves: the prompt before the newest messages is unchanged."""
    middleware = DurableContextMiddleware(volatile_at_tail=True)
    step1 = [SystemMessage(content="sys"), HumanMessage(content="go"), AIMessage(content="", tool_calls=[{"name": "bash", "args": {}, "id": "c1", "type": "tool_call"}]), ToolMessage(content="ok", tool_call_id="c1")]
    first = _inject(middleware, step1)
    state2 = {**STATE, "tool_artifacts": [*STATE["tool_artifacts"], {"handle": "art_2", "display_name": "b.png", "artifact_type": "file", "status": "available"}]}
    step2 = [*step1, AIMessage(content="", tool_calls=[{"name": "bash", "args": {}, "id": "c2", "type": "tool_call"}]), ToolMessage(content="ok2", tool_call_id="c2")]
    second = _inject(middleware, step2, state2)
    assert [m.content for m in second[: len(first) - 1]] == [m.content for m in first[:-1]]


def test_tail_mode_with_nothing_changing_has_no_tail_message():
    state = {"summary_text": "Earlier.", "goal": None}
    messages = _inject(DurableContextMiddleware(volatile_at_tail=True), [SystemMessage(content="sys"), HumanMessage(content="go")], state)
    assert len(_data(messages)) == 1 and messages[-1].content == "go"

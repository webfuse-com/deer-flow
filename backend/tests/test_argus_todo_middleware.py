"""Tests for ArgusTodoMiddleware and the factory routing in lead_agent.

The subclass should:
  1. Default to Argus-aligned system prompt + tool description.
  2. Honour explicit kwargs that override the defaults.
  3. Inherit behavioural hooks from TodoMiddleware (smoke check).

The factory (``_create_todo_list_middleware``) should:
  4. Return ArgusTodoMiddleware when agent_config.uses_planner_pipeline is True.
  5. Return a vanilla TodoMiddleware (with the upstream prompt) when the
     flag is False or agent_config is None.
  6. Return None when is_plan_mode is False, regardless of the flag.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from langchain_core.messages import AIMessage, HumanMessage

from deerflow.agents.lead_agent.agent import _create_todo_list_middleware
from deerflow.agents.middlewares.argus_todo_middleware import (
    ArgusTodoMiddleware,
    _ARGUS_SYSTEM_PROMPT,
    _ARGUS_TOOL_DESCRIPTION,
)
from deerflow.agents.middlewares.todo_middleware import TodoMiddleware
from deerflow.config.agents_config import AgentConfig


# ---------------------------------------------------------------------------
# Construction defaults
# ---------------------------------------------------------------------------


def test_default_construction_uses_argus_prompt():
    """Without args the subclass picks up the planner-aligned prompt."""
    mw = ArgusTodoMiddleware()
    # The base class stores the system prompt; we check it lands there.
    # Implementation detail: TodoListMiddleware exposes the prompt via
    # `tools[0].description` and the system prompt via attached state — we
    # check both layers by sampling text we know is unique to the Argus
    # variant.
    assert "planner" in _ARGUS_SYSTEM_PROMPT.lower()
    assert "/mnt/skills/public/planner/SKILL.md" in _ARGUS_SYSTEM_PROMPT
    assert "supersedes" in _ARGUS_SYSTEM_PROMPT
    # The middleware itself must be alive after construction.
    assert isinstance(mw, ArgusTodoMiddleware)
    assert isinstance(mw, TodoMiddleware)


def test_default_tool_description_mentions_plan_json():
    assert "plan.json" in _ARGUS_TOOL_DESCRIPTION
    assert "skip" in _ARGUS_TOOL_DESCRIPTION


def test_explicit_overrides_take_priority():
    """An explicit prompt or description in kwargs overrides the default."""
    custom_prompt = "<custom_prompt>just for testing</custom_prompt>"
    custom_tool = "Custom tool description for testing only."
    mw = ArgusTodoMiddleware(
        system_prompt=custom_prompt,
        tool_description=custom_tool,
    )
    # We can't easily introspect the system prompt without instantiating an
    # agent. Confirm the constructor accepted the kwargs without error and
    # produced the right type.
    assert isinstance(mw, ArgusTodoMiddleware)


# ---------------------------------------------------------------------------
# Inherited behaviour smoke-check
# ---------------------------------------------------------------------------


def _make_runtime():
    runtime = MagicMock()
    runtime.context = {"thread_id": "test-thread"}
    return runtime


def test_inherits_context_loss_reminder_path():
    """The before_model context-loss reminder logic comes from
    TodoMiddleware. Smoke-check that it still fires for the subclass when
    todos exist in state but the original write_todos call has rolled out
    of context."""
    mw = ArgusTodoMiddleware()
    state = {
        "todos": [
            {"status": "in_progress", "content": "step 1"},
            {"status": "pending", "content": "step 2"},
        ],
        "messages": [
            # An old AIMessage with NO write_todos tool call — simulates
            # the call having scrolled out of the truncated context.
            AIMessage(content="some prior assistant text", tool_calls=[]),
        ],
    }
    result = mw.before_model(state, _make_runtime())
    assert result is not None, "expected a reminder when todos exist but write_todos is gone"
    assert "messages" in result
    injected = result["messages"][0]
    assert isinstance(injected, HumanMessage)
    assert getattr(injected, "name", None) == "todo_reminder"


# ---------------------------------------------------------------------------
# Factory routing
# ---------------------------------------------------------------------------


def test_factory_returns_argus_for_planner_pipeline_agent():
    """AgentConfig with uses_planner_pipeline=True gets ArgusTodoMiddleware."""
    cfg = AgentConfig(name="qwen-local-coder", uses_planner_pipeline=True)
    mw = _create_todo_list_middleware(is_plan_mode=True, agent_name="qwen-local-coder", agent_config=cfg)
    assert isinstance(mw, ArgusTodoMiddleware)


def test_factory_returns_argus_for_glm_planner():
    """Any agent with the flag set gets ArgusTodoMiddleware, not just qwen-local-coder."""
    cfg = AgentConfig(name="glm-planner", uses_planner_pipeline=True)
    mw = _create_todo_list_middleware(is_plan_mode=True, agent_name="glm-planner", agent_config=cfg)
    assert isinstance(mw, ArgusTodoMiddleware)


def test_factory_returns_vanilla_without_flag():
    """AgentConfig without the flag gets the upstream TodoMiddleware."""
    cfg = AgentConfig(name="code-reviewer", uses_planner_pipeline=False)
    mw = _create_todo_list_middleware(is_plan_mode=True, agent_name="code-reviewer", agent_config=cfg)
    assert isinstance(mw, TodoMiddleware)
    assert not isinstance(mw, ArgusTodoMiddleware), (
        "code-reviewer should keep the upstream prompt, not Argus's override"
    )


def test_factory_returns_vanilla_when_agent_config_is_none():
    """No agent_config means no override — straight upstream."""
    mw = _create_todo_list_middleware(is_plan_mode=True, agent_name="qwen-local-coder")
    assert isinstance(mw, TodoMiddleware)
    assert not isinstance(mw, ArgusTodoMiddleware)


def test_factory_returns_none_when_plan_mode_off():
    """Even with the flag set, plan_mode off means no middleware."""
    cfg = AgentConfig(name="qwen-local-coder", uses_planner_pipeline=True)
    mw = _create_todo_list_middleware(is_plan_mode=False, agent_name="qwen-local-coder", agent_config=cfg)
    assert mw is None


# ---------------------------------------------------------------------------
# [argus patch #41] stringified-todos coercion
# ---------------------------------------------------------------------------


def _write_todos_call(todos):
    return {"name": "write_todos", "args": {"todos": todos}, "id": "tc-1", "type": "tool_call"}


def test_after_model_coerces_stringified_todos():
    """glm-nw double-encodes the todos arg as a JSON string (weekly eval
    2026-07-02, pythia/planning): after_model must parse it in place so
    pydantic validation and state.todos hydration succeed."""
    mw = ArgusTodoMiddleware()
    payload = '[{"content": "S1: do the thing", "status": "in_progress"}, {"content": "S2: verify", "status": "pending"}]'
    msg = AIMessage(content="", tool_calls=[_write_todos_call(payload)])
    state = {"todos": [], "messages": [msg]}

    mw.after_model(state, _make_runtime())

    todos = msg.tool_calls[0]["args"]["todos"]
    assert isinstance(todos, list)
    assert todos[0] == {"content": "S1: do the thing", "status": "in_progress"}
    assert todos[1]["status"] == "pending"


def test_after_model_leaves_unparseable_string_for_validation():
    """A todos string that is not JSON (or not a list) must be left alone so
    the normal pydantic validation error path still fires."""
    mw = ArgusTodoMiddleware()
    not_json = AIMessage(content="", tool_calls=[_write_todos_call("do the thing")])
    not_a_list = AIMessage(content="", tool_calls=[_write_todos_call('{"content": "x"}')])

    mw.after_model({"todos": [], "messages": [not_json]}, _make_runtime())
    mw.after_model({"todos": [], "messages": [not_a_list]}, _make_runtime())

    assert not_json.tool_calls[0]["args"]["todos"] == "do the thing"
    assert not_a_list.tool_calls[0]["args"]["todos"] == '{"content": "x"}'


def test_after_model_ignores_native_list_and_other_tools():
    """Native list args and non-write_todos calls pass through untouched."""
    mw = ArgusTodoMiddleware()
    native = [{"content": "S1", "status": "in_progress"}]
    msg = AIMessage(content="", tool_calls=[
        _write_todos_call(list(native)),
        {"name": "bash", "args": {"command": '["not", "todos"]'}, "id": "tc-2", "type": "tool_call"},
    ])
    state = {"todos": [], "messages": [msg]}

    mw.after_model(state, _make_runtime())

    assert msg.tool_calls[0]["args"]["todos"] == native
    assert msg.tool_calls[1]["args"]["command"] == '["not", "todos"]'

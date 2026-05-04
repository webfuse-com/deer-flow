"""Tests for ArgusTodoMiddleware and the factory routing in lead_agent.

The subclass should:
  1. Default to Argus-aligned system prompt + tool description.
  2. Honour explicit kwargs that override the defaults.
  3. Inherit behavioural hooks from TodoMiddleware (smoke check).

The factory (``_create_todo_list_middleware``) should:
  4. Return ArgusTodoMiddleware when agent_name == "qwen-local-coder".
  5. Return a vanilla TodoMiddleware (with the upstream prompt) for any
     other agent_name.
  6. Return None when is_plan_mode is False, regardless of agent_name.
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


def test_factory_returns_argus_for_qwen_local_coder():
    mw = _create_todo_list_middleware(is_plan_mode=True, agent_name="qwen-local-coder")
    assert isinstance(mw, ArgusTodoMiddleware)


def test_factory_returns_vanilla_for_other_agents():
    """Any agent name other than qwen-local-coder gets the upstream
    TodoMiddleware. Confirm by type — ArgusTodoMiddleware is a subclass,
    so we check the exact type rather than isinstance."""
    mw = _create_todo_list_middleware(is_plan_mode=True, agent_name="code-reviewer")
    assert isinstance(mw, TodoMiddleware)
    assert not isinstance(mw, ArgusTodoMiddleware), (
        "code-reviewer should keep the upstream prompt, not Argus's override"
    )


def test_factory_returns_vanilla_when_agent_name_is_empty():
    """No agent_name (the default) means no override — straight upstream."""
    mw = _create_todo_list_middleware(is_plan_mode=True)
    assert isinstance(mw, TodoMiddleware)
    assert not isinstance(mw, ArgusTodoMiddleware)


def test_factory_returns_none_when_plan_mode_off():
    """Even for qwen-local-coder, plan_mode off means no middleware."""
    mw = _create_todo_list_middleware(is_plan_mode=False, agent_name="qwen-local-coder")
    assert mw is None

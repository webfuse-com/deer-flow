"""Adaptive reasoning only downgrades deterministic successful tool turns."""

from types import SimpleNamespace

from langchain_core.messages import HumanMessage, ToolMessage

from deerflow.agents.middlewares.adaptive_reasoning_middleware import AdaptiveReasoningMiddleware


def _request(messages):
    return SimpleNamespace(messages=messages)


def test_successful_routine_tool_turn_uses_routine_mode() -> None:
    middleware = AdaptiveReasoningMiddleware(object(), routine_tools=["read_file", "str_replace"])
    message = ToolMessage(content="ok", tool_call_id="one", name="read_file", status="success")
    message.additional_kwargs["deerflow_tool_meta"] = {"status": "success"}
    assert middleware._routine_turn(_request([HumanMessage("go"), message])) is True


def test_error_or_nonroutine_tool_keeps_reasoning_mode() -> None:
    middleware = AdaptiveReasoningMiddleware(object(), routine_tools=["read_file"])
    error = ToolMessage(content="error", tool_call_id="one", name="read_file", status="error")
    research = ToolMessage(content="ok", tool_call_id="two", name="web_search", status="success")
    assert middleware._routine_turn(_request([error])) is False
    assert middleware._routine_turn(_request([research])) is False


def test_user_turn_keeps_reasoning_mode() -> None:
    middleware = AdaptiveReasoningMiddleware(object(), routine_tools=["read_file"])
    assert middleware._routine_turn(_request([HumanMessage("new requirement")])) is False

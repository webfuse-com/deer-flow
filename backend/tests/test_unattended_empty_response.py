"""[argus patch #44] An unattended run that answers blank ends silently.

Since the 09-23 sync, upstream's model-boundary empty-response recovery
(``LLMErrorHandlingMiddleware``, #5080) retried a blank final once and then
returned a visible fallback that ended the run ``error``, unattended or not.
atlas-nicholas scheduled meeting-prep run 6f150e7d (2026-10-21): local-qwen
answered blank twice, the run ended with "Model returned a completed response
with no content" instead of finishing silently. An unattended run now keeps its
blank final: no retry, no fallback (neither upstream's empty-response one nor
``TerminalResponseMiddleware``'s post-tool one), and the run succeeds; the
channel manager's silence branch suppresses the send. Interactive runs keep
upstream's behaviour.

Also pins how the thinned #37 composes with it: a truly empty interactive final
is sampled twice (upstream's one retry), not four times.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from deerflow.agents.middlewares import llm_error_handling_middleware as llm_module
from deerflow.agents.middlewares.empty_final_retry_middleware import EmptyFinalRetryMiddleware
from deerflow.agents.middlewares.llm_error_handling_middleware import LLMErrorHandlingMiddleware
from deerflow.agents.middlewares.terminal_response_middleware import TerminalResponseMiddleware
from deerflow.config.app_config import AppConfig, LlmCallConfig
from deerflow.config.sandbox_config import SandboxConfig

_FALLBACK_TEXT = "empty response after one automatic retry"


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    llm_module._PROCESS_LIMITER = None
    llm_module._CAP_RESOLVED = False
    monkeypatch.setattr("time.sleep", lambda _delay: None)

    async def _no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    yield
    llm_module._PROCESS_LIMITER = None
    llm_module._CAP_RESOLVED = False


def _llm_errors() -> LLMErrorHandlingMiddleware:
    config = AppConfig(sandbox=SandboxConfig(use="test"), llm_call=LlmCallConfig(retry_max_attempts=3, retry_base_delay_ms=1, retry_cap_delay_ms=1))
    return LLMErrorHandlingMiddleware(app_config=config)


def _request(*, unattended: bool) -> SimpleNamespace:
    context = {"thread_id": "t1", "run_id": "r1"}
    if unattended:
        context["unattended"] = True
    return SimpleNamespace(runtime=SimpleNamespace(context=context))


def _blank() -> AIMessage:
    return AIMessage(content="", response_metadata={"finish_reason": "stop"})


class TestModelBoundary:
    def test_unattended_blank_final_is_kept_without_retry(self):
        calls = []

        def handler(_request):
            calls.append(1)
            return _blank()

        result = _llm_errors().wrap_model_call(_request(unattended=True), handler)

        assert len(calls) == 1
        assert result.content == ""
        assert "deerflow_error_fallback" not in result.additional_kwargs

    @pytest.mark.anyio
    async def test_unattended_blank_final_is_kept_without_retry_async(self):
        calls = []

        async def handler(_request):
            calls.append(1)
            return _blank()

        result = await _llm_errors().awrap_model_call(_request(unattended=True), handler)

        assert len(calls) == 1
        assert result.content == ""
        assert "deerflow_error_fallback" not in result.additional_kwargs

    def test_interactive_blank_final_keeps_upstreams_retry_and_fallback(self):
        calls = []

        def handler(_request):
            calls.append(1)
            return _blank()

        result = _llm_errors().wrap_model_call(_request(unattended=False), handler)

        assert len(calls) == 2
        assert result.additional_kwargs["deerflow_error_fallback"] is True
        assert result.additional_kwargs["error_reason"] == "empty_response"
        assert _FALLBACK_TEXT in result.content

    @pytest.mark.anyio
    async def test_interactive_blank_final_keeps_upstreams_retry_and_fallback_async(self):
        calls = []

        async def handler(_request):
            calls.append(1)
            return _blank()

        result = await _llm_errors().awrap_model_call(_request(unattended=False), handler)

        assert len(calls) == 2
        assert result.additional_kwargs["deerflow_error_fallback"] is True
        assert _FALLBACK_TEXT in result.content

    def test_unattended_response_without_any_message_still_retries(self):
        # Not a blank final but a malformed provider result: upstream's recovery stays.
        calls = []

        def handler(_request):
            calls.append(1)
            return AIMessage(content="ok") if len(calls) == 2 else SimpleNamespace(result=[])

        result = _llm_errors().wrap_model_call(_request(unattended=True), handler)

        assert len(calls) == 2
        assert result.content == "ok"


class TestPostToolFallback:
    @staticmethod
    def _state() -> dict:
        return {
            "messages": [
                HumanMessage(content="prepare my meetings"),
                AIMessage(content="", tool_calls=[{"id": "c1", "name": "calendar", "args": {}}]),
                ToolMessage(content="no meetings", tool_call_id="c1"),
                AIMessage(content=""),
            ]
        }

    def test_unattended_post_tool_blank_gets_no_fallback(self):
        assert TerminalResponseMiddleware().after_model(self._state(), _request(unattended=True).runtime) is None

    def test_interactive_post_tool_blank_still_gets_the_fallback(self):
        update = TerminalResponseMiddleware().after_model(self._state(), _request(unattended=False).runtime)
        assert update["messages"][0].additional_kwargs["deerflow_error_fallback"] is True


@tool
def calendar(day: str) -> str:
    """Pretend to read the calendar."""
    return f"no meetings on {day}"


class _ToolThenBlankModel(BaseChatModel):
    """Calls the calendar once, then answers blank on every later call."""

    call_count: int = 0

    @property
    def _llm_type(self) -> str:
        return "tool-then-blank"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.call_count += 1
        if self.call_count == 1:
            message = AIMessage(content="", tool_calls=[{"id": "c1", "name": "calendar", "args": {"day": "today"}}], response_metadata={"finish_reason": "tool_calls"})
        else:
            message = AIMessage(content="", response_metadata={"finish_reason": "stop"})
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def _graph(model):
    # Lead-chain relative order: LLM error handling (runtime tail) is outer to
    # EmptyFinalRetry, which is outer to TerminalResponse.
    return create_agent(model=model, tools=[calendar], middleware=[_llm_errors(), EmptyFinalRetryMiddleware(), TerminalResponseMiddleware()], context_schema=dict)


@pytest.mark.anyio
async def test_unattended_scheduled_run_with_nothing_to_say_ends_silently():
    """The 6f150e7d shape: tools, then a blank final, on a scheduled run."""
    model = _ToolThenBlankModel()
    result = await _graph(model).ainvoke(
        {"messages": [HumanMessage(content="Prepare my meetings for today.")]},
        context={"thread_id": "t1", "run_id": "6f150e7d", "unattended": True},
    )

    final = result["messages"][-1]
    assert isinstance(final, AIMessage) and final.content == ""
    assert "deerflow_error_fallback" not in final.additional_kwargs
    assert model.call_count == 2  # the tool call and one blank answer, no retry


@pytest.mark.anyio
async def test_interactive_run_keeps_the_retry_and_the_visible_error():
    model = _ToolThenBlankModel()
    result = await _graph(model).ainvoke(
        {"messages": [HumanMessage(content="Prepare my meetings for today.")]},
        context={"thread_id": "t1", "run_id": "r-interactive"},
    )

    final = result["messages"][-1]
    assert final.additional_kwargs["deerflow_error_fallback"] is True
    assert _FALLBACK_TEXT in str(final.content)
    # One blank answer plus upstream's one retry; the thinned #37 adds no
    # samples of its own for an empty final (it used to make this up to four).
    assert model.call_count == 3

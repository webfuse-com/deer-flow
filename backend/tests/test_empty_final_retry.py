"""[argus patch #37] Tests for the blank-final-turn fixes:

  * ``deerflow.utils.messages.is_blank_text`` — the shared emptiness check now
    homed in the harness (was ``app.channels.manager._is_blank_text``).
  * ``app.gateway.pagination.mark_blank_final_ai_messages`` — the web-side
    display guard.
  * ``EmptyFinalRetryMiddleware`` — re-invoke the model once on a blank final.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.gateway.pagination import mark_blank_final_ai_messages
from deerflow.agents.middlewares.empty_final_retry_middleware import (
    EmptyFinalRetryMiddleware,
    _is_blank_final,
)
from deerflow.utils.messages import NO_RESPONSE_MARKER, is_blank_text


class TestIsBlankText:
    @pytest.mark.parametrize("content", ["", "   ", "\n\n", ".", "..", "-", "…", None])
    def test_blank_and_filler(self, content):
        assert is_blank_text(content) is True

    @pytest.mark.parametrize("content", ["ok", "No meetings today.", "42", "a."])
    def test_real_content(self, content):
        assert is_blank_text(content) is False

    def test_list_content_shape(self):
        # LangChain list-of-blocks content: blank when no text blocks carry text.
        assert is_blank_text([{"type": "text", "text": "  "}]) is True
        assert is_blank_text([{"type": "text", "text": "hello"}]) is False


class TestMarkBlankFinalAiMessages:
    def _msg(self, content, run_id="r1", event_type="ai_message"):
        return {"event_type": event_type, "run_id": run_id, "content": content}

    def test_blank_final_gets_marker(self):
        msgs = [self._msg("hi", event_type="human_message"), self._msg("\n\n")]
        mark_blank_final_ai_messages(msgs, {1})
        assert msgs[1]["content"] == NO_RESPONSE_MARKER

    def test_real_final_untouched(self):
        msgs = [self._msg("Here is the answer.")]
        mark_blank_final_ai_messages(msgs, {0})
        assert msgs[0]["content"] == "Here is the answer."

    def test_only_indices_in_set_are_touched(self):
        # An empty AI message NOT flagged as a run's last (e.g. an intermediate
        # turn) must be left alone.
        msgs = [self._msg(""), self._msg("real")]
        mark_blank_final_ai_messages(msgs, {1})  # index 0 not in set
        assert msgs[0]["content"] == ""
        assert msgs[1]["content"] == "real"

    def test_non_ai_message_at_index_ignored(self):
        msgs = [self._msg("", event_type="human_message")]
        mark_blank_final_ai_messages(msgs, {0})
        assert msgs[0]["content"] == ""

    def test_out_of_range_index_safe(self):
        msgs = [self._msg("x")]
        mark_blank_final_ai_messages(msgs, {5})  # no error
        assert msgs[0]["content"] == "x"


def _response(messages):
    """Minimal ModelResponse stand-in: only `.result` is read by the middleware."""
    return SimpleNamespace(result=messages)


class TestIsBlankFinal:
    def test_blank_final_ai(self):
        assert _is_blank_final(_response([AIMessage(content="")])) is True
        assert _is_blank_final(_response([AIMessage(content="\n\n")])) is True

    def test_real_final_ai(self):
        assert _is_blank_final(_response([AIMessage(content="answer")])) is False

    def test_blank_but_has_tool_calls_is_not_final(self):
        # A blank message carrying tool_calls is a normal intermediate turn.
        msg = AIMessage(content="", tool_calls=[{"name": "bash", "id": "c1", "args": {}}])
        assert _is_blank_final(_response([msg])) is False

    def test_non_ai_last_message(self):
        assert _is_blank_final(_response([HumanMessage(content="")])) is False

    def test_empty_result(self):
        assert _is_blank_final(_response([])) is False


class TestEmptyFinalRetryMiddleware:
    def _req(self):
        return MagicMock(name="ModelRequest")

    def test_sync_retries_once_and_returns_nonblank(self):
        mw = EmptyFinalRetryMiddleware()
        blank = _response([AIMessage(content="")])
        good = _response([AIMessage(content="real answer")])
        handler = MagicMock(side_effect=[blank, good])
        out = mw.wrap_model_call(self._req(), handler)
        assert handler.call_count == 2
        assert out is good

    def test_sync_no_retry_when_first_is_good(self):
        mw = EmptyFinalRetryMiddleware()
        good = _response([AIMessage(content="real")])
        handler = MagicMock(side_effect=[good])
        out = mw.wrap_model_call(self._req(), handler)
        assert handler.call_count == 1
        assert out is good

    def test_sync_retry_also_blank_returns_blank_no_loop(self):
        mw = EmptyFinalRetryMiddleware()
        blank1 = _response([AIMessage(content="")])
        blank2 = _response([AIMessage(content="   ")])
        handler = MagicMock(side_effect=[blank1, blank2])
        out = mw.wrap_model_call(self._req(), handler)
        assert handler.call_count == 2  # exactly one retry, never loops
        assert out is blank2

    def test_sync_no_retry_on_tool_call_turn(self):
        mw = EmptyFinalRetryMiddleware()
        tool_turn = _response([AIMessage(content="", tool_calls=[{"name": "bash", "id": "c1", "args": {}}])])
        handler = MagicMock(side_effect=[tool_turn])
        out = mw.wrap_model_call(self._req(), handler)
        assert handler.call_count == 1
        assert out is tool_turn

    @pytest.mark.asyncio
    async def test_async_retries_once_and_returns_nonblank(self):
        mw = EmptyFinalRetryMiddleware()
        blank = _response([AIMessage(content="\n")])
        good = _response([AIMessage(content="real")])
        handler = AsyncMock(side_effect=[blank, good])
        out = await mw.awrap_model_call(self._req(), handler)
        assert handler.await_count == 2
        assert out is good

    @pytest.mark.asyncio
    async def test_async_no_retry_when_good(self):
        mw = EmptyFinalRetryMiddleware()
        good = _response([AIMessage(content="real")])
        handler = AsyncMock(side_effect=[good])
        out = await mw.awrap_model_call(self._req(), handler)
        assert handler.await_count == 1
        assert out is good

"""Unit tests for ViewImageMiddleware.

Tests cover the middleware's ability to inject image details (including base64
payloads) into the model request, triggered only when the previous assistant
turn contained `view_image` tool calls that have all been completed with
corresponding ToolMessages.

Covered behavior:
- `_get_last_assistant_message` returns the most recent AIMessage (or None).
- `_has_view_image_tool` only matches assistant messages with `view_image` tool calls.
- `_all_tools_completed` verifies every tool call id has a matching ToolMessage.
- `_create_image_details_message` produces correctly structured content blocks,
  reading image files on-demand from disk (no base64 stored in state).
- `_should_inject_image_message` gates injection on all preconditions, including
  deduplication when an image-details message is already in the request.
- `_inject` rebuilds the request's image context: it sweeps out any copy left
  in the message list by an interrupted run before deciding whether to append a
  freshly built one.
- `wrap_model_call` and `awrap_model_call` expose the same behavior sync/async,
  handing the payload to the model without ever writing it to state — so no
  checkpoint retains it, even if the run is interrupted mid-call.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware.types import ModelRequest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage

from deerflow.agents.middlewares.view_image_middleware import (
    _IMAGE_CONTEXT_MESSAGE_MARKER_KEY,
    ViewImageMiddleware,
)


def _view_image_call(call_id: str = "call_1", path: str = "/mnt/user-data/uploads/img.png") -> dict:
    return {"name": "view_image", "id": call_id, "args": {"image_path": path}}


def _other_tool_call(call_id: str = "call_other", name: str = "bash") -> dict:
    return {"name": name, "id": call_id, "args": {"command": "ls"}}


def _model_request(messages: list[AnyMessage], viewed_images: dict | None = None) -> ModelRequest:
    """Build a real ModelRequest so `.override()` behaves as it does in the graph."""
    return ModelRequest(
        model=FakeMessagesListChatModel(responses=[AIMessage(content="ok")]),
        messages=list(messages),
        system_message=None,
        tool_choice=None,
        tools=[],
        response_format=None,
        state={"messages": list(messages), "viewed_images": viewed_images or {}},
        runtime=MagicMock(),
        model_settings={},
    )


class _CaptureChatMessages(BaseCallbackHandler):
    def __init__(self):
        self.messages = []

    def on_chat_model_start(self, serialized, messages, **kwargs):
        self.messages = messages[0]


def _image_context_messages(messages: list[AnyMessage]) -> list[HumanMessage]:
    return [message for message in messages if isinstance(message, HumanMessage) and message.id and message.id.startswith("view-image-context:")]


def _make_viewed_image(tmp_path, filename="img.png", mime_type="image/png", data=b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"):
    """Create a real image file and return viewed_images metadata dict."""
    img_path = tmp_path / filename
    img_path.write_bytes(data)
    return {
        "mime_type": mime_type,
        "size": len(data),
        "actual_path": str(img_path),
    }


class TestGetLastAssistantMessage:
    def test_returns_none_on_empty_list(self):
        mw = ViewImageMiddleware()
        assert mw._get_last_assistant_message([]) is None

    def test_returns_none_when_no_ai_message(self):
        mw = ViewImageMiddleware()
        messages = [
            SystemMessage(content="sys"),
            HumanMessage(content="hi"),
        ]
        assert mw._get_last_assistant_message(messages) is None

    def test_returns_most_recent_ai_message(self):
        mw = ViewImageMiddleware()
        older = AIMessage(content="older")
        newer = AIMessage(content="newer")
        messages = [HumanMessage(content="q"), older, HumanMessage(content="q2"), newer]
        assert mw._get_last_assistant_message(messages) is newer


class TestHasViewImageTool:
    def test_returns_false_when_tool_calls_attr_missing(self):
        """Exercise the `not hasattr(message, "tool_calls")` guard.

        AIMessage always has a `tool_calls` attribute, so we use a plain
        object that truly lacks the attribute to cover this branch.
        """
        mw = ViewImageMiddleware()
        msg = SimpleNamespace(content="just text")  # no tool_calls attribute
        assert not hasattr(msg, "tool_calls")  # precondition
        assert mw._has_view_image_tool(msg) is False

    def test_returns_false_when_ai_message_has_no_tool_calls(self):
        """AIMessage without tool_calls kwarg defaults to an empty list."""
        mw = ViewImageMiddleware()
        msg = AIMessage(content="just text")
        assert mw._has_view_image_tool(msg) is False

    def test_returns_false_when_tool_calls_empty(self):
        mw = ViewImageMiddleware()
        msg = AIMessage(content="", tool_calls=[])
        assert mw._has_view_image_tool(msg) is False

    def test_returns_true_when_view_image_present(self):
        mw = ViewImageMiddleware()
        msg = AIMessage(content="", tool_calls=[_view_image_call()])
        assert mw._has_view_image_tool(msg) is True

    def test_returns_true_when_view_image_mixed_with_others(self):
        mw = ViewImageMiddleware()
        msg = AIMessage(
            content="",
            tool_calls=[_other_tool_call(), _view_image_call(call_id="call_vi")],
        )
        assert mw._has_view_image_tool(msg) is True

    def test_returns_false_when_only_other_tools(self):
        mw = ViewImageMiddleware()
        msg = AIMessage(content="", tool_calls=[_other_tool_call()])
        assert mw._has_view_image_tool(msg) is False


class TestAllToolsCompleted:
    def test_returns_false_when_no_tool_calls(self):
        mw = ViewImageMiddleware()
        assistant = AIMessage(content="", tool_calls=[])
        assert mw._all_tools_completed([assistant], assistant) is False

    def test_returns_true_when_all_completed(self):
        mw = ViewImageMiddleware()
        assistant = AIMessage(
            content="",
            tool_calls=[_view_image_call("c1"), _view_image_call("c2", "/p2.png")],
        )
        messages = [
            assistant,
            ToolMessage(content="ok", tool_call_id="c1"),
            ToolMessage(content="ok", tool_call_id="c2"),
        ]
        assert mw._all_tools_completed(messages, assistant) is True

    def test_returns_false_when_some_tool_call_unanswered(self):
        mw = ViewImageMiddleware()
        assistant = AIMessage(
            content="",
            tool_calls=[_view_image_call("c1"), _view_image_call("c2", "/p2.png")],
        )
        messages = [assistant, ToolMessage(content="ok", tool_call_id="c1")]
        assert mw._all_tools_completed(messages, assistant) is False

    def test_returns_false_when_assistant_not_in_messages(self):
        mw = ViewImageMiddleware()
        assistant = AIMessage(content="", tool_calls=[_view_image_call("c1")])
        # assistant is not part of the list, so messages.index() will raise and be caught
        messages = [HumanMessage(content="hi")]
        assert mw._all_tools_completed(messages, assistant) is False

    def test_ignores_tool_messages_before_assistant(self):
        mw = ViewImageMiddleware()
        assistant = AIMessage(content="", tool_calls=[_view_image_call("c1")])
        # A stale ToolMessage with matching id appears BEFORE the assistant turn.
        # It should not count — only ToolMessages after the assistant close the call.
        messages = [
            ToolMessage(content="stale", tool_call_id="c1"),
            assistant,
        ]
        assert mw._all_tools_completed(messages, assistant) is False


class TestCreateImageDetailsMessage:
    def test_returns_placeholder_when_no_images(self):
        mw = ViewImageMiddleware()
        state = {"viewed_images": {}}
        blocks = mw._create_image_details_message(state)
        assert blocks == [{"type": "text", "text": "No images have been viewed."}]

    def test_returns_placeholder_when_state_missing_key(self):
        mw = ViewImageMiddleware()
        blocks = mw._create_image_details_message({})
        assert blocks == [{"type": "text", "text": "No images have been viewed."}]

    def test_builds_blocks_for_single_image(self, tmp_path):
        mw = ViewImageMiddleware()
        img_meta = _make_viewed_image(tmp_path, "cat.png")
        state = {
            "viewed_images": {
                "/path/to/cat.png": img_meta,
            }
        }
        blocks = mw._create_image_details_message(state)

        # header text + per-image description text + per-image image_url block
        assert len(blocks) == 3
        assert blocks[0] == {"type": "text", "text": "Here are the images you've viewed:"}
        assert blocks[1]["type"] == "text"
        assert "/path/to/cat.png" in blocks[1]["text"]
        assert "image/png" in blocks[1]["text"]
        assert blocks[2]["type"] == "image_url"
        assert blocks[2]["image_url"]["url"].startswith("data:image/png;base64,")

    def test_builds_blocks_for_multiple_images(self, tmp_path):
        mw = ViewImageMiddleware()
        img1 = _make_viewed_image(tmp_path, "a.png", data=b"\x89PNG\r\n\x1a\nfake-png")
        img2 = _make_viewed_image(tmp_path, "b.jpg", mime_type="image/jpeg", data=b"\xff\xd8\xff\xe0fake-jpeg")
        state = {
            "viewed_images": {
                "/a.png": img1,
                "/b.jpg": img2,
            }
        }
        blocks = mw._create_image_details_message(state)

        # 1 header + (1 description + 1 image_url) per image = 5 blocks
        assert len(blocks) == 5
        image_url_blocks = [b for b in blocks if isinstance(b, dict) and b.get("type") == "image_url"]
        assert len(image_url_blocks) == 2
        urls = {b["image_url"]["url"] for b in image_url_blocks}
        assert any(u.startswith("data:image/png;base64,") for u in urls)
        assert any(u.startswith("data:image/jpeg;base64,") for u in urls)

    def test_omits_image_url_block_when_file_missing(self, tmp_path):
        mw = ViewImageMiddleware()
        state = {
            "viewed_images": {
                "/broken.png": {
                    "mime_type": "image/png",
                    "size": 0,
                    "actual_path": str(tmp_path / "nonexistent.png"),
                },
            }
        }
        blocks = mw._create_image_details_message(state)
        # header + description + error text (file no longer available)
        assert len(blocks) == 3
        assert all(not (isinstance(b, dict) and b.get("type") == "image_url") for b in blocks)

    def test_uses_unknown_mime_type_when_missing(self, tmp_path):
        mw = ViewImageMiddleware()
        img_meta = _make_viewed_image(tmp_path, "mystery.bin", mime_type="unknown")
        state = {
            "viewed_images": {
                "/mystery.bin": img_meta,
            }
        }
        blocks = mw._create_image_details_message(state)
        # The description block should mention unknown
        description_blocks = [b for b in blocks if b.get("type") == "text" and "/mystery.bin" in b.get("text", "")]
        assert len(description_blocks) == 1
        assert "unknown" in description_blocks[0]["text"]

    def test_omits_image_url_when_read_raises_oserror(self, tmp_path, monkeypatch):
        """A failure during on-demand read must not crash the middleware."""
        img_meta = _make_viewed_image(tmp_path, "ok.png")
        state = {
            "viewed_images": {
                "/ok.png": img_meta,
            }
        }

        def _raise(*args, **kwargs):
            raise OSError("disk error")

        monkeypatch.setattr("builtins.open", _raise)

        mw = ViewImageMiddleware()
        blocks = mw._create_image_details_message(state)
        # header + description + 'unavailable' text, no image_url block
        assert all(not (isinstance(b, dict) and b.get("type") == "image_url") for b in blocks)
        unavailable = [b for b in blocks if isinstance(b, dict) and b.get("type") == "text" and "unavailable" in b.get("text", "")]
        assert len(unavailable) == 1

    def test_omits_image_url_when_size_changes_between_view_and_inject(self, tmp_path):
        """Defense against TOCTOU growth: skip if current size differs from recorded size."""
        img_meta = _make_viewed_image(tmp_path, "shrinking.png", data=b"original-larger-content")
        # Grow the file after the metadata was written
        img_meta_path = Path(img_meta["actual_path"])
        img_meta_path.write_bytes(b"much-much-much-larger-content-bytes")

        state = {"viewed_images": {"/shrinking.png": img_meta}}
        mw = ViewImageMiddleware()
        blocks = mw._create_image_details_message(state)
        assert all(not (isinstance(b, dict) and b.get("type") == "image_url") for b in blocks)

    def test_omits_image_url_when_size_exceeds_cap(self, tmp_path):
        """Records a small size but the actual file is large - the cap kicks in regardless."""
        img_meta = _make_viewed_image(tmp_path, "huge.png", data=b"x" * 100)
        img_meta_path = Path(img_meta["actual_path"])
        # Grow past the cap (20 MB)
        img_meta_path.write_bytes(b"y" * (21 * 1024 * 1024))

        state = {"viewed_images": {"/huge.png": img_meta}}
        mw = ViewImageMiddleware()
        blocks = mw._create_image_details_message(state)
        assert all(not (isinstance(b, dict) and b.get("type") == "image_url") for b in blocks)


class TestShouldInjectImageMessage:
    def test_false_when_no_messages(self):
        mw = ViewImageMiddleware()
        assert mw._should_inject_image_message([]) is False

    def test_false_when_no_assistant_message(self):
        mw = ViewImageMiddleware()
        assert mw._should_inject_image_message([HumanMessage(content="hello")]) is False

    def test_false_when_no_view_image_tool_call(self):
        mw = ViewImageMiddleware()
        assistant = AIMessage(content="", tool_calls=[_other_tool_call()])
        messages = [assistant, ToolMessage(content="ok", tool_call_id="call_other")]
        assert mw._should_inject_image_message(messages) is False

    def test_false_when_tool_not_completed(self):
        mw = ViewImageMiddleware()
        assistant = AIMessage(content="", tool_calls=[_view_image_call("c1")])
        assert mw._should_inject_image_message([assistant]) is False  # no ToolMessage yet

    def test_true_when_all_preconditions_met(self):
        mw = ViewImageMiddleware()
        assistant = AIMessage(content="", tool_calls=[_view_image_call("c1")])
        messages = [assistant, ToolMessage(content="ok", tool_call_id="c1")]
        assert mw._should_inject_image_message(messages) is True

    def test_false_when_already_injected(self):
        """A checkpoint written by an older version (or by a run that died before
        its `RemoveMessage` cleanup landed) can still carry image details. Do not
        add a duplicate on top of one."""
        mw = ViewImageMiddleware()
        assistant = AIMessage(content="", tool_calls=[_view_image_call("c1")])
        already_injected = HumanMessage(content="Here are the images you've viewed: /img.png")
        messages = [
            assistant,
            ToolMessage(content="ok", tool_call_id="c1"),
            already_injected,
        ]
        assert mw._should_inject_image_message(messages) is False

    def test_false_when_already_injected_with_list_content(self, tmp_path):
        """Deduplication must recognize the real injected payload shape.

        An unmarked leftover carries `.content` as a *list* of dicts (text +
        image_url blocks), not a plain string. This test reuses
        `_create_image_details_message` output to reproduce the realistic shape
        and confirms the marker is still detected via `str(msg.content)`.
        """
        mw = ViewImageMiddleware()
        assistant = AIMessage(content="", tool_calls=[_view_image_call("c1")])
        viewed_images = {"/img.png": _make_viewed_image(tmp_path)}
        # Build content the same way the middleware would.
        real_injected_content = mw._create_image_details_message({"viewed_images": viewed_images})
        # Sanity: this is a list of blocks, not a plain string.
        assert isinstance(real_injected_content, list)

        messages = [
            assistant,
            ToolMessage(content="ok", tool_call_id="c1"),
            HumanMessage(content=real_injected_content),
        ]
        assert mw._should_inject_image_message(messages) is False

    def test_false_when_legacy_details_marker_present(self):
        """The middleware also recognizes the legacy 'Here are the details of the
        images you've viewed' marker as an already-injected signal."""
        mw = ViewImageMiddleware()
        assistant = AIMessage(content="", tool_calls=[_view_image_call("c1")])
        legacy = HumanMessage(content="Here are the details of the images you've viewed: ...")
        messages = [
            assistant,
            ToolMessage(content="ok", tool_call_id="c1"),
            legacy,
        ]
        assert mw._should_inject_image_message(messages) is False


class TestInject:
    def test_returns_request_unchanged_when_should_not_inject(self):
        mw = ViewImageMiddleware()
        request = _model_request([HumanMessage(content="hi")])
        assert mw._inject(request) is request

    def test_appends_image_context_message_to_request(self, tmp_path):
        mw = ViewImageMiddleware()
        assistant = AIMessage(content="", tool_calls=[_view_image_call("c1")])
        original = [assistant, ToolMessage(content="ok", tool_call_id="c1")]
        request = _model_request(original, {"/img.png": _make_viewed_image(tmp_path)})

        injected_request = mw._inject(request)

        assert injected_request is not request
        # The payload is appended last, so it directly follows the tool results.
        assert injected_request.messages[:-1] == original
        injected = injected_request.messages[-1]
        assert isinstance(injected, HumanMessage)
        # Mixed-content payload: list of text + image_url blocks
        assert isinstance(injected.content, list)
        assert any(isinstance(b, dict) and b.get("type") == "image_url" for b in injected.content)
        # Internal injection: must be hidden from the chat UI (and IM channels),
        # like the other middleware-injected context messages.
        assert injected.additional_kwargs.get("hide_from_ui") is True
        assert injected.additional_kwargs.get(_IMAGE_CONTEXT_MESSAGE_MARKER_KEY) is True
        assert injected.id is not None
        assert injected.id.startswith("view-image-context:")

    def test_replaces_a_stranded_payload_instead_of_stacking_a_second_one(self, tmp_path):
        """A run that died during the model call can leave the old
        before_model/after_model pair's message checkpointed. Rebuild it rather
        than adding a second copy on top."""
        mw = ViewImageMiddleware()
        assistant = AIMessage(content="", tool_calls=[_view_image_call("c1")])
        stranded = ViewImageMiddleware._create_image_context_message([{"type": "text", "text": "stale"}])
        request = _model_request(
            [assistant, ToolMessage(content="ok", tool_call_id="c1"), stranded],
            {"/img.png": _make_viewed_image(tmp_path)},
        )

        injected = _image_context_messages(mw._inject(request).messages)

        assert len(injected) == 1
        assert injected[0].id != stranded.id
        assert any(isinstance(b, dict) and b.get("type") == "image_url" for b in injected[0].content)

    def test_drops_a_stranded_payload_even_when_no_injection_is_warranted(self):
        """Otherwise the stale base64 would ride along in every later request for
        the life of the thread -- the old `after_model` swept it, so dropping the
        hook must not lose that."""
        mw = ViewImageMiddleware()
        stranded = ViewImageMiddleware._create_image_context_message([{"type": "text", "text": "stale"}])
        request = _model_request([HumanMessage(content="hi"), stranded, AIMessage(content="done")])

        prepared = mw._inject(request)

        assert prepared is not request
        assert _image_context_messages(prepared.messages) == []
        assert [type(m) for m in prepared.messages] == [HumanMessage, AIMessage]

    def test_never_drops_a_client_message_wearing_the_reserved_prefix(self):
        """The prefix alone is not enough — Gateway strips the server-owned
        marker from client input, and both are required to match."""
        mw = ViewImageMiddleware()
        client_message = HumanMessage(id="view-image-context:client-supplied", content="client-authored", additional_kwargs={"hide_from_ui": True})
        request = _model_request([client_message, AIMessage(content="done")])

        assert mw._inject(request) is request

    def test_does_not_mutate_the_incoming_request(self, tmp_path):
        mw = ViewImageMiddleware()
        assistant = AIMessage(content="", tool_calls=[_view_image_call("c1")])
        request = _model_request(
            [assistant, ToolMessage(content="ok", tool_call_id="c1")],
            {"/img.png": _make_viewed_image(tmp_path)},
        )

        mw._inject(request)

        assert _image_context_messages(request.messages) == []


class TestWrapModelCall:
    def test_handler_receives_the_image_context_message(self, tmp_path):
        mw = ViewImageMiddleware()
        assistant = AIMessage(content="", tool_calls=[_view_image_call("c1")])
        request = _model_request(
            [assistant, ToolMessage(content="ok", tool_call_id="c1")],
            {"/img.png": _make_viewed_image(tmp_path)},
        )
        seen: list[ModelRequest] = []

        def handler(prepared: ModelRequest) -> AIMessage:
            seen.append(prepared)
            return AIMessage(content="I can see the image.")

        result = mw.wrap_model_call(request, handler)

        assert result.content == "I can see the image."
        assert len(_image_context_messages(seen[0].messages)) == 1

    def test_handler_receives_request_unchanged_when_not_warranted(self):
        mw = ViewImageMiddleware()
        request = _model_request([HumanMessage(content="hi")])
        seen: list[ModelRequest] = []

        mw.wrap_model_call(request, lambda prepared: seen.append(prepared) or AIMessage(content="ok"))

        assert seen[0] is request

    @pytest.mark.anyio
    async def test_awrap_model_call_matches_sync_behavior(self, tmp_path):
        mw = ViewImageMiddleware()
        assistant = AIMessage(content="", tool_calls=[_view_image_call("c1")])
        request = _model_request(
            [assistant, ToolMessage(content="ok", tool_call_id="c1")],
            {"/img.png": _make_viewed_image(tmp_path)},
        )
        seen: list[ModelRequest] = []

        async def handler(prepared: ModelRequest) -> AIMessage:
            seen.append(prepared)
            return AIMessage(content="I can see the image.")

        result = await mw.awrap_model_call(request, handler)

        assert result.content == "I can see the image."
        assert len(_image_context_messages(seen[0].messages)) == 1


class TestGraphIntegration:
    def _graph_and_capture(self):
        capture = _CaptureChatMessages()
        model = FakeMessagesListChatModel(
            responses=[AIMessage(content="I can see the image.")],
            callbacks=[capture],
        )
        return create_agent(model=model, tools=[], middleware=[ViewImageMiddleware()]), capture

    def _input(self, tmp_path):
        return {
            "messages": [
                AIMessage(content="", tool_calls=[_view_image_call("c1")]),
                ToolMessage(content="ok", tool_call_id="c1"),
            ],
            "viewed_images": {"/img.png": _make_viewed_image(tmp_path)},
        }

    def test_image_context_reaches_the_model_but_never_the_state(self, tmp_path):
        graph, capture = self._graph_and_capture()

        result = graph.invoke(self._input(tmp_path))

        model_image_messages = _image_context_messages(capture.messages)
        assert len(model_image_messages) == 1
        assert any(block.get("type") == "image_url" for block in model_image_messages[0].content)
        # Nothing is written back, so the payload is absent from every checkpoint
        # rather than being added and then removed again.
        assert _image_context_messages(result["messages"]) == []

    @pytest.mark.anyio
    async def test_async_graph_matches_sync_behavior(self, tmp_path):
        graph, capture = self._graph_and_capture()

        result = await graph.ainvoke(self._input(tmp_path))

        assert len(_image_context_messages(capture.messages)) == 1
        assert _image_context_messages(result["messages"]) == []

    def test_graph_preserves_normalized_client_message_with_reserved_prefix(self, tmp_path):
        from app.gateway.services import normalize_input

        client_id = "view-image-context:client-supplied"
        normalized = normalize_input(
            {
                "messages": [
                    {
                        "role": "user",
                        "id": client_id,
                        "content": "client-authored message",
                        "additional_kwargs": {
                            _IMAGE_CONTEXT_MESSAGE_MARKER_KEY: True,
                            "custom": "keep-me",
                        },
                    }
                ]
            }
        )
        client_message = normalized["messages"][0]
        assert _IMAGE_CONTEXT_MESSAGE_MARKER_KEY not in client_message.additional_kwargs

        graph, capture = self._graph_and_capture()
        graph_input = self._input(tmp_path)

        result = graph.invoke({**graph_input, "messages": [client_message, *graph_input["messages"]]})

        assert any(message.id == client_id for message in capture.messages)
        assert any(message.id != client_id and message.additional_kwargs.get(_IMAGE_CONTEXT_MESSAGE_MARKER_KEY) is True for message in _image_context_messages(capture.messages))
        persisted_client = next(message for message in result["messages"] if message.id == client_id)
        assert persisted_client.content == "client-authored message"
        assert persisted_client.additional_kwargs == {"custom": "keep-me"}
        assert all(message.additional_kwargs.get(_IMAGE_CONTEXT_MESSAGE_MARKER_KEY) is not True for message in result["messages"] if isinstance(message, HumanMessage))


class TestVisionDescribeForNonVisionLead:
    """[argus patch #20] When constructed with vision_model_name (lead model is
    non-vision), awrap_model_call injects a TEXT description produced by the
    vision model instead of the raw image, so render-and-verify works on
    non-vision leads."""

    def _completed_request(self, tmp_path):
        assistant = AIMessage(content="", tool_calls=[_view_image_call("c1")])
        return _model_request(
            [assistant, ToolMessage(content="ok", tool_call_id="c1")],
            {"/img.png": _make_viewed_image(tmp_path)},
        )

    def _patch_model(self, monkeypatch, description="a red circle on white"):
        import deerflow.models as models_mod

        class _FakeModel:
            async def ainvoke(self, messages):
                return SimpleNamespace(content=description)

        monkeypatch.setattr(models_mod, "create_chat_model", lambda **kwargs: _FakeModel())

    def test_sync_wrap_defers_for_non_vision_lead(self, tmp_path):
        """The sync path must NOT inject (it cannot await the describe call)."""
        mw = ViewImageMiddleware(vision_model_name="local-qwen")
        request = self._completed_request(tmp_path)
        seen: list[ModelRequest] = []

        mw.wrap_model_call(request, lambda prepared: seen.append(prepared) or AIMessage(content="ok"))

        assert seen[0] is request
        assert _image_context_messages(seen[0].messages) == []

    @pytest.mark.anyio
    async def test_async_injects_text_description(self, monkeypatch, tmp_path):
        self._patch_model(monkeypatch)
        mw = ViewImageMiddleware(vision_model_name="local-qwen")
        request = self._completed_request(tmp_path)
        seen: list[ModelRequest] = []

        async def handler(prepared: ModelRequest) -> AIMessage:
            seen.append(prepared)
            return AIMessage(content="ok")

        await mw.awrap_model_call(request, handler)

        injected = _image_context_messages(seen[0].messages)
        assert len(injected) == 1
        msg = injected[0]
        # Injected content is TEXT blocks describing the image, not image_url.
        text = " ".join(b["text"] for b in msg.content if isinstance(b, dict) and b.get("type") == "text")
        assert "a red circle on white" in text
        assert not any(isinstance(b, dict) and b.get("type") == "image_url" for b in msg.content)
        # Internal context: hidden from chat UI / IM channels, never written to state.
        assert msg.additional_kwargs.get("hide_from_ui") is True
        assert msg.additional_kwargs.get(_IMAGE_CONTEXT_MESSAGE_MARKER_KEY) is True
        assert _image_context_messages(request.messages) == []

    @pytest.mark.anyio
    async def test_describe_failure_is_best_effort(self, monkeypatch, tmp_path):
        import deerflow.models as models_mod

        class _BoomModel:
            async def ainvoke(self, messages):
                raise RuntimeError("vision endpoint down")

        monkeypatch.setattr(models_mod, "create_chat_model", lambda **kwargs: _BoomModel())
        mw = ViewImageMiddleware(vision_model_name="local-qwen")
        seen: list[ModelRequest] = []

        async def handler(prepared: ModelRequest) -> AIMessage:
            seen.append(prepared)
            return AIMessage(content="ok")

        await mw.awrap_model_call(self._completed_request(tmp_path), handler)

        # A describe failure must not abort the turn; it injects a placeholder.
        injected = _image_context_messages(seen[0].messages)
        assert len(injected) == 1
        text = " ".join(b["text"] for b in injected[0].content if isinstance(b, dict))
        assert "vision description unavailable" in text

    @pytest.mark.anyio
    async def test_unreadable_image_injects_placeholder(self, monkeypatch, tmp_path):
        self._patch_model(monkeypatch)
        mw = ViewImageMiddleware(vision_model_name="local-qwen")
        assistant = AIMessage(content="", tool_calls=[_view_image_call("c1")])
        request = _model_request(
            [assistant, ToolMessage(content="ok", tool_call_id="c1")],
            {"/img.png": {"mime_type": "image/png", "size": 5, "actual_path": str(tmp_path / "missing.png")}},
        )
        seen: list[ModelRequest] = []

        async def handler(prepared: ModelRequest) -> AIMessage:
            seen.append(prepared)
            return AIMessage(content="ok")

        await mw.awrap_model_call(request, handler)

        injected = _image_context_messages(seen[0].messages)
        assert len(injected) == 1
        text = " ".join(b["text"] for b in injected[0].content if isinstance(b, dict))
        assert "file unavailable or changed" in text

    @pytest.mark.anyio
    async def test_vision_capable_lead_injects_raw_image(self, tmp_path):
        """vision_model_name=None -> upstream behavior: inject the raw image."""
        mw = ViewImageMiddleware()  # vision lead
        seen: list[ModelRequest] = []

        async def handler(prepared: ModelRequest) -> AIMessage:
            seen.append(prepared)
            return AIMessage(content="ok")

        await mw.awrap_model_call(self._completed_request(tmp_path), handler)

        injected = _image_context_messages(seen[0].messages)
        assert len(injected) == 1
        assert any(isinstance(b, dict) and b.get("type") == "image_url" for b in injected[0].content)


def _png_bytes(width: int, height: int, mode: str = "RGB") -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new(mode, (width, height)).save(buffer, format="PNG")
    return buffer.getvalue()


def _data_url_size(url: str) -> tuple[int, int]:
    import base64
    import io

    from PIL import Image

    with Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))) as image:
        return image.size


class TestManyImageLimits:
    """[argus patch #97] Anthropic rejects >20-image requests with any side over
    2000 px, and every view_image call re-sends every viewed image, so a long
    visual-review thread 400'd on every later view (atlas-nicholas 0e223809);
    on local-qwen the same thread shape outlived the request timeout
    (atlas-nicholas 60e3181f). Limits: the ``_MAX_*`` constants."""

    @staticmethod
    def _scaled(width, height):
        from deerflow.agents.middlewares.view_image_middleware import _MAX_IMAGE_EDGE_PX as edge

        scale = edge / max(width, height)
        return (edge, round(height * scale)) if width >= height else (round(width * scale), edge)

    def test_tall_image_is_downscaled_and_labelled(self, tmp_path):
        from deerflow.agents.middlewares.view_image_middleware import _MAX_IMAGE_EDGE_PX

        meta = _make_viewed_image(tmp_path, "tall.png", data=_png_bytes(1600, 7391))
        blocks = ViewImageMiddleware()._create_image_details_message({"viewed_images": {"/tall.png": meta}})

        image_blocks = [b for b in blocks if b.get("type") == "image_url"]
        assert len(image_blocks) == 1
        assert _data_url_size(image_blocks[0]["image_url"]["url"]) == self._scaled(1600, 7391)
        assert f"downscaled from 1600x7391 to fit {_MAX_IMAGE_EDGE_PX} px" in blocks[1]["text"]

    def test_image_within_limit_is_sent_unchanged(self, tmp_path):
        data = _png_bytes(1200, 900)
        meta = _make_viewed_image(tmp_path, "ok.png", data=data)
        blocks = ViewImageMiddleware()._create_image_details_message({"viewed_images": {"/ok.png": meta}})

        url = next(b for b in blocks if b.get("type") == "image_url")["image_url"]["url"]
        assert url.endswith(__import__("base64").b64encode(data).decode())
        assert "downscaled" not in blocks[1]["text"]

    def test_opaque_downscale_is_sent_as_jpeg(self, tmp_path):
        meta = _make_viewed_image(tmp_path, "p.png", data=_png_bytes(300, 2500, mode="P"))
        blocks = ViewImageMiddleware()._create_image_details_message({"viewed_images": {"/p.png": meta}})

        url = next(b for b in blocks if b.get("type") == "image_url")["image_url"]["url"]
        assert url.startswith("data:image/jpeg;base64,")
        assert _data_url_size(url) == self._scaled(300, 2500)

    def test_transparent_downscale_stays_png(self, tmp_path):
        meta = _make_viewed_image(tmp_path, "t.png", data=_png_bytes(2400, 300, mode="RGBA"))
        blocks = ViewImageMiddleware()._create_image_details_message({"viewed_images": {"/t.png": meta}})

        url = next(b for b in blocks if b.get("type") == "image_url")["image_url"]["url"]
        assert url.startswith("data:image/png;base64,")
        assert _data_url_size(url) == self._scaled(2400, 300)

    def test_only_the_most_recent_images_are_resent(self, tmp_path):
        from deerflow.agents.middlewares.view_image_middleware import _MAX_CONTEXT_IMAGES as cap

        viewed = {f"/img{i}.png": _make_viewed_image(tmp_path, f"img{i}.png", data=_png_bytes(8, 8)) for i in range(24)}
        blocks = ViewImageMiddleware()._create_image_details_message({"viewed_images": viewed})

        assert blocks[0] == {"type": "text", "text": "Here are the images you've viewed:"}
        assert blocks[1]["text"].startswith(f"({24 - cap} earlier viewed image(s) not re-sent")
        assert sum(1 for b in blocks if b.get("type") == "image_url") == cap
        named = [b["text"] for b in blocks if b.get("type") == "text" and "**/img" in b["text"]]
        assert f"**/img{24 - cap}.png**" in named[0] and "**/img23.png**" in named[-1]

    def test_reviewing_an_image_moves_it_to_the_recent_end(self):
        from deerflow.agents.thread_state import merge_viewed_images

        merged = merge_viewed_images({"/a": {"size": 1}, "/b": {"size": 2}}, {"/a": {"size": 3}})
        assert list(merged) == ["/b", "/a"]
        assert merged["/a"] == {"size": 3}

    def test_describe_path_gets_downscaled_recent_images(self, tmp_path):
        from deerflow.agents.middlewares.view_image_middleware import _MAX_CONTEXT_IMAGES as cap

        viewed = {f"/img{i}.png": _make_viewed_image(tmp_path, f"img{i}.png", data=_png_bytes(8, 8)) for i in range(21)}
        viewed["/tall.png"] = _make_viewed_image(tmp_path, "tall.png", data=_png_bytes(500, 4000))
        inputs = ViewImageMiddleware(vision_model_name="local-qwen")._describe_inputs({"viewed_images": viewed})

        assert len(inputs) == cap
        path, mime_type, _, b64_data = inputs[-1]
        assert path == "/tall.png" and mime_type == "image/jpeg"
        assert _data_url_size(f"data:{mime_type};base64,{b64_data}") == self._scaled(500, 4000)


class TestTimeoutRetryWithoutImages:
    """[argus patch #97] A timed-out call that carried images is retried once without them."""

    class APITimeoutError(Exception):
        pass

    @staticmethod
    def _request_with_images():
        mw = ViewImageMiddleware()
        image_msg = mw._create_image_context_message([{"type": "text", "text": "Here are the images you've viewed:"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}])
        request = MagicMock()
        request.messages = [HumanMessage(content="hi"), image_msg]
        request.override = lambda **kw: SimpleNamespace(messages=kw["messages"])
        return mw, request

    def test_timeout_retries_once_without_images(self):
        mw, request = self._request_with_images()
        calls = []

        def handler(req):
            calls.append(req)
            if len(calls) == 1:
                raise self.APITimeoutError("Request timed out.")
            return "ok"

        mw._inject = lambda r: r
        assert mw.wrap_model_call(request, handler) == "ok"
        retried = calls[1].messages
        assert not any(isinstance(b, dict) and b.get("type") == "image_url" for m in retried for b in (m.content if isinstance(m.content, list) else []))
        assert "1 viewed image(s) were not sent" in retried[-1].content[0]["text"]

    def test_async_timeout_wrapped_in_another_error_retries(self):
        mw, request = self._request_with_images()
        calls = []

        async def handler(req):
            calls.append(req)
            if len(calls) == 1:
                try:
                    raise self.APITimeoutError("Request timed out.")
                except Exception as exc:
                    raise RuntimeError("wrapped") from exc
            return "ok"

        mw._inject = lambda r: r
        assert asyncio.run(mw.awrap_model_call(request, handler)) == "ok"
        assert len(calls) == 2

    def test_non_timeout_error_is_not_retried(self):
        mw, request = self._request_with_images()
        mw._inject = lambda r: r

        def handler(req):
            raise ValueError("400 bad request")

        with pytest.raises(ValueError):
            mw.wrap_model_call(request, handler)

    def test_timeout_without_images_is_not_retried(self):
        mw = ViewImageMiddleware()
        request = MagicMock()
        request.messages = [HumanMessage(content="hi")]
        mw._inject = lambda r: r
        calls = []

        def handler(req):
            calls.append(req)
            raise self.APITimeoutError("Request timed out.")

        with pytest.raises(self.APITimeoutError):
            mw.wrap_model_call(request, handler)
        assert len(calls) == 1

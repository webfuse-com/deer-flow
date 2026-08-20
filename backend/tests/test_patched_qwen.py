from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage

from deerflow.models.patched_qwen import PatchedChatQwen


def _make_model(**kwargs) -> PatchedChatQwen:
    return PatchedChatQwen(
        model="Qwen/Qwen3.6-27B-FP8",
        api_key="test-key",
        base_url="https://example.com/v1",
        **kwargs,
    )


def test_create_chat_result_extracts_reasoning_content():
    """SGLang/vLLM return chain-of-thought as message.reasoning_content; the
    stock adapter drops it. Ours lands it in additional_kwargs so it persists
    in the checkpointer and can re-enter history."""
    model = _make_model()
    response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "104729 = 317 x 331, so no.",
                    "reasoning_content": "Check small factors: 104729 / 7 ...",
                },
                "finish_reason": "stop",
                "index": 0,
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }

    result = model._create_chat_result(response)

    message = result.generations[0].message
    assert message.content == "104729 = 317 x 331, so no."
    assert message.additional_kwargs["reasoning_content"] == "Check small factors: 104729 / 7 ..."


def test_create_chat_result_without_reasoning_is_untouched():
    model = _make_model()
    response = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "pong"},
                "finish_reason": "stop",
                "index": 0,
            }
        ],
    }

    result = model._create_chat_result(response)

    message = result.generations[0].message
    assert message.content == "pong"
    assert "reasoning_content" not in message.additional_kwargs


def test_stream_chunks_extract_and_accumulate_reasoning():
    """Stream deltas carry incremental reasoning_content; chunk accumulation
    must concatenate the fragments into the full trace."""
    model = _make_model()
    chunks = [
        {"choices": [{"delta": {"role": "assistant", "reasoning_content": "Let me "}, "index": 0}]},
        {"choices": [{"delta": {"reasoning_content": "check factors."}, "index": 0}]},
        {"choices": [{"delta": {"content": "The answer is no."}, "index": 0}]},
    ]

    accumulated = None
    for chunk in chunks:
        generation_chunk = model._convert_chunk_to_generation_chunk(chunk, AIMessageChunk, None)
        assert generation_chunk is not None
        accumulated = generation_chunk.message if accumulated is None else accumulated + generation_chunk.message

    assert accumulated is not None
    assert accumulated.additional_kwargs.get("reasoning_content") == "Let me check factors."
    assert accumulated.content == "The answer is no."


def test_request_payload_reinjects_reasoning_when_preserve_requested():
    """Stock serialisation drops unknown additional_kwargs; with
    preserve_thinking in the request the field must be re-attached or the
    template has nothing to preserve."""
    model = _make_model(extra_body={"chat_template_kwargs": {"enable_thinking": True, "preserve_thinking": True}})

    payload = model._get_request_payload(
        [
            SystemMessage(content="system"),
            HumanMessage(content="is 104729 prime?"),
            AIMessage(
                content="no, 317 x 331",
                additional_kwargs={"reasoning_content": "Check small factors ..."},
            ),
            HumanMessage(content="are you sure?"),
        ]
    )

    wire = payload["messages"]
    assistant = [m for m in wire if m["role"] == "assistant"]
    assert len(assistant) == 1
    assert assistant[0]["reasoning_content"] == "Check small factors ..."
    # Non-assistant messages never gain the field.
    assert all("reasoning_content" not in m for m in wire if m["role"] != "assistant")


def test_request_payload_omits_reasoning_without_preserve():
    """Without preserve_thinking in the request the payload is byte-identical
    to stock ChatOpenAI: the server template strips history reasoning anyway,
    so sending it would only grow the wire payload."""
    model = _make_model(extra_body={"chat_template_kwargs": {"enable_thinking": True}})

    payload = model._get_request_payload(
        [
            HumanMessage(content="is 104729 prime?"),
            AIMessage(
                content="no, 317 x 331",
                additional_kwargs={"reasoning_content": "Check small factors ..."},
            ),
            HumanMessage(content="are you sure?"),
        ]
    )

    assert all("reasoning_content" not in m for m in payload["messages"])


def test_request_payload_reinjects_with_top_level_chat_template_kwargs():
    """preserve_thinking at the top level (not nested in extra_body) also
    enables re-injection."""
    model = _make_model()

    payload = model._get_request_payload(
        [
            HumanMessage(content="q"),
            AIMessage(content="a", additional_kwargs={"reasoning_content": "r"}),
        ],
        chat_template_kwargs={"preserve_thinking": True},
    )

    assistant = [m for m in payload["messages"] if m["role"] == "assistant"]
    assert assistant[0]["reasoning_content"] == "r"

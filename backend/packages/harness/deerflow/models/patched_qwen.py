"""Patched ChatOpenAI adapter for Qwen reasoning preservation (preserve_thinking).

Qwen3.x served by SGLang/vLLM separates chain-of-thought into a
``reasoning_content`` field on the response message (and on each stream delta).
Stock ``langchain_openai.ChatOpenAI`` deliberately does not extract that field
(see langchain_openai/chat_models/base.py: "reasoning_content ... not
extracted"), so the reasoning never lands on the AIMessage, never reaches the
checkpointer, and never re-enters the conversation history.

Qwen3.6's chat template supports ``preserve_thinking``: when enabled, historical
assistant messages keep their ``<think>`` blocks in the rendered prompt instead
of being stripped (upstream default keeps only the current turn's reasoning).
Qwen recommends this for agent loops: decision consistency across turns, and in
many cases lower total token consumption because the model does not re-derive
prior reasoning.

This adapter closes both halves of the loop:

1. Inbound: extract ``reasoning_content`` from responses (stream + non-stream)
   into ``additional_kwargs["reasoning_content"]`` so it persists in history.
2. Outbound: re-inject ``reasoning_content`` onto assistant wire messages, but
   ONLY when the request itself asks for ``preserve_thinking`` (via
   ``chat_template_kwargs`` at either nesting level). Stock
   ``_convert_message_to_dict`` drops unknown additional_kwargs, so without
   this override the field would never reach the server. Gating on the kwarg
   keeps the class inert for callers that do not opt in: extraction alone
   changes nothing server-side (the template strips history reasoning unless
   preserve_thinking is set).

Wire it in config.yaml instead of ``langchain_openai:ChatOpenAI`` and add
``preserve_thinking: true`` to the thinking overlay::

    - name: local-qwen
      use: deerflow.models.patched_qwen:PatchedChatQwen
      ...
      when_thinking_enabled:
        extra_body:
          chat_template_kwargs:
            enable_thinking: true
            preserve_thinking: true
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI


def _with_reasoning_content(
    message: AIMessage | AIMessageChunk,
    reasoning: str,
) -> AIMessage | AIMessageChunk:
    """Attach reasoning text to a message, appending within one chunk.

    Cross-chunk accumulation is handled by LangChain itself:
    ``add_ai_message_chunks`` concatenates string additional_kwargs via
    ``merge_dicts``, so per-delta fragments assemble into the full trace.
    """
    additional_kwargs = dict(message.additional_kwargs)
    existing = additional_kwargs.get("reasoning_content")
    additional_kwargs["reasoning_content"] = f"{existing}{reasoning}" if isinstance(existing, str) else reasoning
    return message.model_copy(update={"additional_kwargs": additional_kwargs})


def _preserve_thinking_requested(payload: dict) -> bool:
    """True when the outgoing payload asks the template to preserve thinking."""
    extra_body = payload.get("extra_body")
    for container in (extra_body if isinstance(extra_body, dict) else {}, payload):
        ctk = container.get("chat_template_kwargs")
        if isinstance(ctk, dict) and ctk.get("preserve_thinking") is True:
            return True
    return False


class PatchedChatQwen(ChatOpenAI):
    """ChatOpenAI adapter that round-trips Qwen ``reasoning_content``."""

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict,
        default_chunk_class: type,
        base_generation_info: dict | None,
    ) -> ChatGenerationChunk | None:
        generation_chunk = super()._convert_chunk_to_generation_chunk(chunk, default_chunk_class, base_generation_info)
        if generation_chunk is None:
            return None
        choices = chunk.get("choices") or []
        if not choices:
            return generation_chunk
        delta = choices[0].get("delta")
        if not isinstance(delta, Mapping):
            return generation_chunk
        reasoning = delta.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning and isinstance(generation_chunk.message, AIMessageChunk):
            generation_chunk.message = _with_reasoning_content(generation_chunk.message, reasoning)
        return generation_chunk

    def _create_chat_result(
        self,
        response: dict | Any,
        generation_info: dict | None = None,
    ) -> ChatResult:
        result = super()._create_chat_result(response, generation_info)
        response_dict = response if isinstance(response, dict) else response.model_dump()
        choices = response_dict.get("choices", [])

        generations: list[ChatGeneration] = []
        for index, generation in enumerate(result.generations):
            choice = choices[index] if index < len(choices) else {}
            message = generation.message
            if isinstance(message, AIMessage) and isinstance(choice, Mapping):
                choice_message = choice.get("message", {})
                if isinstance(choice_message, Mapping):
                    reasoning = choice_message.get("reasoning_content")
                    if isinstance(reasoning, str) and reasoning.strip():
                        message = _with_reasoning_content(message, reasoning)
                        generation = ChatGeneration(
                            message=message,
                            generation_info=generation.generation_info,
                        )
            generations.append(generation)

        return ChatResult(generations=generations, llm_output=result.llm_output)

    def _get_request_payload(
        self,
        input_: LanguageModelInput,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict:
        """Re-inject reasoning_content onto assistant messages when preserving.

        Stock ``_convert_message_to_dict`` cherry-picks additional_kwargs keys
        (tool_calls, function_call, audio) and drops everything else, so the
        field must be re-attached here. Gated on the request's own
        preserve_thinking kwarg: without it the server template strips history
        reasoning anyway, and the payload stays byte-identical to stock.
        """
        original_messages = self._convert_input(input_).to_messages()
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if not _preserve_thinking_requested(payload):
            return payload
        messages = payload.get("messages")
        if not isinstance(messages, list) or len(messages) != len(original_messages):
            return payload
        for original, wire in zip(original_messages, messages):
            if isinstance(original, AIMessage) and isinstance(wire, dict) and wire.get("role") == "assistant":
                reasoning = original.additional_kwargs.get("reasoning_content")
                if isinstance(reasoning, str) and reasoning.strip():
                    wire["reasoning_content"] = reasoning
        return payload

"""Middleware for injecting image details into conversation before LLM call."""

import asyncio
import base64
import logging
from pathlib import Path
from typing import override
from uuid import uuid4

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage
from langgraph.runtime import Runtime

from deerflow.agents.thread_state import ThreadState

logger = logging.getLogger(__name__)

# Mirror the tool-side size cap as a defense-in-depth check. The tool
# enforces this at write time; the middleware re-checks at read time in
# case the file grew on disk between view and injection.
_MAX_IMAGE_BYTES = 20 * 1024 * 1024
_IMAGE_CONTEXT_MESSAGE_ID_PREFIX = "view-image-context:"
_IMAGE_CONTEXT_MESSAGE_MARKER_KEY = "deerflow_view_image_context"


class ViewImageMiddlewareState(ThreadState):
    """Reuse the thread state so reducer-backed keys keep their annotations."""


class ViewImageMiddleware(AgentMiddleware[ViewImageMiddlewareState]):
    """Injects image details as a human message before LLM calls when view_image tools have completed.

    This middleware:
    1. Runs before each LLM call
    2. Checks if the last assistant message contains view_image tool calls
    3. Verifies all tool calls in that message have been completed (have corresponding ToolMessages)
    4. If conditions are met, creates a human message with all viewed image details (including base64 data)
    5. Adds the message to state so the LLM can see and analyze the images
    6. Removes the transient message after the LLM call so later checkpoints do not retain its base64 data

    This enables the LLM to automatically receive and analyze images that were loaded via view_image tool,
    without requiring explicit user prompts to describe the images.
    """

    state_schema = ViewImageMiddlewareState

    # [argus] Render-verification-focused describe prompt: when the lead model is
    # NOT vision-capable, a vision model (Qwen) describes the screenshot and we
    # inject that TEXT so the lead can act on visual defects it cannot see.
    _DESCRIBE_PROMPT = (
        "Describe this rendered screenshot for a developer verifying their UI. "
        "State, specifically and literally: the overall layout, ALL visible text "
        "verbatim, the colors used, and ANY rendering problems you can see - blank "
        "or all-white areas, overlapping or cut-off elements, error messages, and "
        "missing or broken images. If it looks correct, say so plainly. Do not "
        "speculate about code; report only what is visible."
    )

    def __init__(self, *, vision_model_name: str | None = None, app_config=None) -> None:
        """[argus] vision_model_name: when set, the LEAD model is non-vision, so
        route each viewed image through this vision-capable model (e.g.
        local-qwen) for a TEXT description that is injected instead of the raw
        image. When None, the lead model is vision-capable and the image is
        injected directly (the original behavior)."""
        super().__init__()
        self._vision_model_name = vision_model_name
        self._app_config = app_config

    @staticmethod
    def _is_image_context_message(message: object) -> bool:
        """Return whether a message is trusted transient image context."""
        return isinstance(message, HumanMessage) and bool(message.id) and message.id.startswith(_IMAGE_CONTEXT_MESSAGE_ID_PREFIX) and message.additional_kwargs.get(_IMAGE_CONTEXT_MESSAGE_MARKER_KEY) is True

    def _get_last_assistant_message(self, messages: list) -> AIMessage | None:
        for msg in reversed(messages):
            if isinstance(msg, AIMessage):
                return msg
        return None

    def _has_view_image_tool(self, message: AIMessage) -> bool:
        if not hasattr(message, "tool_calls") or not message.tool_calls:
            return False
        return any(tool_call.get("name") == "view_image" for tool_call in message.tool_calls)

    def _all_tools_completed(self, messages: list, assistant_msg: AIMessage) -> bool:
        if not hasattr(assistant_msg, "tool_calls") or not assistant_msg.tool_calls:
            return False
        tool_call_ids = {tool_call["id"] for tool_call in assistant_msg.tool_calls if "id" in tool_call}
        if not tool_call_ids:
            return False
        try:
            assistant_idx = messages.index(assistant_msg)
        except ValueError:
            return False
        subsequent_messages = messages[assistant_idx + 1 :]
        completed_tool_call_ids = {msg.tool_call_id for msg in subsequent_messages if isinstance(msg, ToolMessage) and hasattr(msg, "tool_call_id")}
        return tool_call_ids.issubset(completed_tool_call_ids)

    def _should_inject_image_message(self, state: ViewImageMiddlewareState) -> bool:
        messages = state.get("messages", [])
        if not messages:
            return False
        last_assistant_msg = self._get_last_assistant_message(messages)
        if not last_assistant_msg or not self._has_view_image_tool(last_assistant_msg):
            return False
        if not self._all_tools_completed(messages, last_assistant_msg):
            return False

        # Deduplication: check if image context was already added after last assistant message
        assistant_idx = messages.index(last_assistant_msg)
        for msg in messages[assistant_idx + 1 :]:
            if isinstance(msg, HumanMessage):
                if self._is_image_context_message(msg):
                    return False
                content_str = str(msg.content)
                if "Here are the images you've viewed" in content_str or "Here are the details of the images you've viewed" in content_str:
                    return False

        return True

    @staticmethod
    def _read_image_as_data_url(actual_path: str, mime_type: str, expected_size: int, legacy_base64: str | None = None) -> str | None:
        """Read image file and return a `data:` URL, or None on failure."""
        if legacy_base64:
            return f"data:{mime_type};base64,{legacy_base64}"
        try:
            file_path = Path(actual_path)
            if not file_path.exists() or not file_path.is_file():
                return None
            current_size = file_path.stat().st_size
            if expected_size and current_size != expected_size:
                return None
            if current_size > _MAX_IMAGE_BYTES:
                return None
            with open(file_path, "rb") as f:
                image_bytes = f.read()
            base64_data = base64.b64encode(image_bytes).decode("utf-8")
            return f"data:{mime_type};base64,{base64_data}"
        except OSError:
            return None

    def _create_image_details_message(self, state: ViewImageMiddlewareState) -> list[str | dict]:
        """Create formatted content blocks for viewed images (sync)."""
        viewed_images = state.get("viewed_images", {})
        if not viewed_images:
            return [{"type": "text", "text": "No images have been viewed."}]

        content_blocks: list[str | dict] = [{"type": "text", "text": "Here are the images you've viewed:"}]

        for image_path, image_data in viewed_images.items():
            mime_type = image_data.get("mime_type", "unknown")
            actual_path = image_data.get("actual_path", "")
            expected_size = image_data.get("size", 0)
            legacy_base64 = image_data.get("base64")

            content_blocks.append({"type": "text", "text": f"\n- **{image_path}** ({mime_type})"})

            if actual_path or legacy_base64:
                data_url = self._read_image_as_data_url(actual_path, mime_type, expected_size, legacy_base64=legacy_base64)
                if data_url:
                    content_blocks.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url},
                        }
                    )
                else:
                    content_blocks.append({"type": "text", "text": f"  (file unavailable or changed on disk: {actual_path})"})

        return content_blocks

    async def _describe_image(self, path: Path, mime_type: str, b64_data: str) -> str:
        try:
            from deerflow.models import create_chat_model
            model = create_chat_model(name=self._vision_model_name, app_config=self._app_config)
            content = [
                {"type": "text", "text": self._DESCRIBE_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64_data}"}},
            ]
            response = await model.ainvoke([HumanMessage(content=content)])
            text = response.content if isinstance(response.content, str) else str(response.content)
            return f"**Visual description of `{path.name}`** (rendered by vision model `{self._vision_model_name}`):\n\n{text}"
        except Exception as e:
            logger.warning("[view_image_middleware] vision description failed for %s: %s", path, e)
            return f"**Visual description of `{path.name}`**: (vision description unavailable: {e})"

    @staticmethod
    def _create_image_context_message(content: list[str | dict]) -> HumanMessage:
        return HumanMessage(
            id=f"{_IMAGE_CONTEXT_MESSAGE_ID_PREFIX}{uuid4().hex}",
            content=content,
            additional_kwargs={
                "hide_from_ui": True,
                _IMAGE_CONTEXT_MESSAGE_MARKER_KEY: True,
            },
        )

    @staticmethod
    def _remove_image_context_messages(state: ViewImageMiddlewareState) -> dict | None:
        removals = [RemoveMessage(id=msg.id) for msg in state.get("messages", []) if ViewImageMiddleware._is_image_context_message(msg)]
        if not removals:
            return None
        return {"messages": removals}

    def _inject_image_message(self, state: ViewImageMiddlewareState) -> dict | None:
        if not self._should_inject_image_message(state):
            return None

        # Non-vision lead model cannot run describe in sync before_model
        if self._vision_model_name:
            return None

        image_content = self._create_image_details_message(state)
        human_msg = self._create_image_context_message(image_content)
        return {"messages": [human_msg]}

    async def _ainject_image_message(self, state: ViewImageMiddlewareState) -> dict | None:
        if not self._should_inject_image_message(state):
            return None

        viewed_images = state.get("viewed_images", {})
        if not viewed_images:
            return None

        if self._vision_model_name:
            text_blocks = []
            for image_path, image_data in viewed_images.items():
                actual_path = image_data.get("actual_path", "")
                mime_type = image_data.get("mime_type", "unknown")
                expected_size = image_data.get("size", 0)
                legacy_base64 = image_data.get("base64")
                path = Path(actual_path or image_path)
                data_url = self._read_image_as_data_url(actual_path, mime_type, expected_size, legacy_base64=legacy_base64)
                if data_url and "," in data_url:
                    b64_data = data_url.split(",", 1)[1]
                    desc = await self._describe_image(path, mime_type, b64_data)
                    text_blocks.append({"type": "text", "text": desc})
                else:
                    text_blocks.append({"type": "text", "text": f"**Visual description of `{path.name}`**: (file unavailable on disk: {actual_path})"})
            human_msg = self._create_image_context_message(text_blocks)
            return {"messages": [human_msg]}
        else:
            image_content = await asyncio.to_thread(self._create_image_details_message, state)
            human_msg = self._create_image_context_message(image_content)
            return {"messages": [human_msg]}

    @override
    def before_model(self, state: ViewImageMiddlewareState, runtime: Runtime) -> dict | None:
        return self._inject_image_message(state)

    @override
    async def abefore_model(self, state: ViewImageMiddlewareState, runtime: Runtime) -> dict | None:
        return await self._ainject_image_message(state)

    @override
    def after_model(self, state: ViewImageMiddlewareState, runtime: Runtime) -> dict | None:
        return self._remove_image_context_messages(state)

    @override
    async def aafter_model(self, state: ViewImageMiddlewareState, runtime: Runtime) -> dict | None:
        return self._remove_image_context_messages(state)


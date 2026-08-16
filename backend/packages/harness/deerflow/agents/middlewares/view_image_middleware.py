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
        assistant_idx = messages.index(assistant_msg)
        subsequent_messages = messages[assistant_idx + 1 :]
        completed_tool_call_ids = {msg.tool_call_id for msg in subsequent_messages if isinstance(msg, ToolMessage) and hasattr(msg, "tool_call_id")}
        return tool_call_ids.issubset(completed_tool_call_ids)

    def _extract_image_paths(self, assistant_msg: AIMessage) -> list[str]:
        image_paths = []
        for tool_call in assistant_msg.tool_calls:
            if tool_call.get("name") == "view_image":
                args = tool_call.get("args", {})
                if isinstance(args, dict):
                    image_path = args.get("image_path") or args.get("image_paths")
                    if isinstance(image_path, str):
                        image_paths.append(image_path)
                    elif isinstance(image_path, list):
                        image_paths.extend([p for p in image_path if isinstance(p, str)])
        return image_paths

    def _should_inject_image_message(self, state: ViewImageMiddlewareState) -> bool:
        messages = state.get("messages", [])
        if not messages:
            return False
        last_assistant_msg = self._get_last_assistant_message(messages)
        if not last_assistant_msg or not self._has_view_image_tool(last_assistant_msg):
            return False
        return self._all_tools_completed(messages, last_assistant_msg)

    async def _describe_image(self, path: Path, mime_type: str, b64_data: str) -> str:
        try:
            from deerflow.models import create_model
            model = create_model(model_name=self._vision_model_name, app_config=self._app_config)
            content = [
                {"type": "text", "text": self._DESCRIBE_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64_data}"}},
            ]
            response = await model.ainvoke([HumanMessage(content=content)])
            text = response.content if isinstance(response.content, str) else str(response.content)
            return f"**Visual description of `{path.name}`** (rendered by vision model `{self._vision_model_name}`):\n\n{text}"
        except Exception as e:
            logger.warning("[view_image_middleware] vision description failed for %s: %s", path, e)
            return f"**Visual description of `{path.name}`**: (vision model description failed: {e})"

    def _create_image_content_sync(self, image_paths: list[str]) -> list[dict]:
        content_items = []
        for raw_path in image_paths:
            path = Path(raw_path)
            if not path.exists():
                logger.warning("[view_image_middleware] image path does not exist: %s", raw_path)
                continue
            try:
                size = path.stat().st_size
                if size > _MAX_IMAGE_BYTES:
                    logger.warning("[view_image_middleware] image exceeds size cap (%d > %d): %s", size, _MAX_IMAGE_BYTES, raw_path)
                    continue
                suffix = path.suffix.lower()
                mime_type = "image/png" if suffix == ".png" else "image/jpeg" if suffix in (".jpg", ".jpeg") else "image/webp" if suffix == ".webp" else "image/gif" if suffix == ".gif" else "application/octet-stream"
                b64_data = base64.b64encode(path.read_bytes()).decode("utf-8")
                content_items.append({"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64_data}"}, "_path": path, "_mime": mime_type, "_b64": b64_data})
            except Exception as e:
                logger.warning("[view_image_middleware] failed to read image %s: %s", raw_path, e)
        return content_items

    async def _ainject_image_message(self, state: ViewImageMiddlewareState) -> dict | None:
        messages = state.get("messages", [])
        last_assistant_msg = self._get_last_assistant_message(messages)
        if not last_assistant_msg:
            return None
        image_paths = self._extract_image_paths(last_assistant_msg)
        if not image_paths:
            return None
        content_items = await asyncio.to_thread(self._create_image_content_sync, image_paths)
        if not content_items:
            return None
        if self._vision_model_name:
            text_blocks = []
            for item in content_items:
                text_blocks.append(await self._describe_image(item["_path"], item["_mime"], item["_b64"]))
            msg = HumanMessage(
                id=f"{_IMAGE_CONTEXT_MESSAGE_ID_PREFIX}{uuid4()}",
                content="\n\n---\n\n".join(text_blocks),
                additional_kwargs={_IMAGE_CONTEXT_MESSAGE_MARKER_KEY: True},
            )
        else:
            cleaned_items = [{"type": item["type"], "image_url": item["image_url"]} for item in content_items]
            msg = HumanMessage(
                id=f"{_IMAGE_CONTEXT_MESSAGE_ID_PREFIX}{uuid4()}",
                content=cleaned_items,
                additional_kwargs={_IMAGE_CONTEXT_MESSAGE_MARKER_KEY: True},
            )
        return {"messages": [msg]}

    @override
    async def abefore_agent(self, state: ViewImageMiddlewareState, runtime: Runtime) -> dict | None:
        if not self._should_inject_image_message(state):
            return None
        return await self._ainject_image_message(state)

    @override
    async def aafter_agent(self, state: ViewImageMiddlewareState, runtime: Runtime) -> dict | None:
        messages = state.get("messages", [])
        removals = [RemoveMessage(id=msg.id) for msg in messages if self._is_image_context_message(msg)]
        return {"messages": removals} if removals else None

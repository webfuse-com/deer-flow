"""Middleware for injecting image details into the model request."""

import base64
import hashlib
import io
import logging
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import override
from uuid import uuid4

from deerflow_extension_api import ContentKind, provenance_kwargs
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage

from deerflow.agents.thread_state import ThreadState
from deerflow.sandbox.lease import run_sync_lifecycle_operation

logger = logging.getLogger(__name__)

# Mirror the tool-side size cap as a defense-in-depth check. The tool
# enforces this at write time; the middleware re-checks at read time in
# case the file grew between view and injection.
_MAX_IMAGE_BYTES = 20 * 1024 * 1024
_IMAGE_CONTEXT_MESSAGE_ID_PREFIX = "view-image-context:"
_IMAGE_CONTEXT_MESSAGE_MARKER_KEY = "deerflow_view_image_context"

# [argus patch #97] Anthropic models reject a request carrying more than 20
# images when any of them is over 2000 px on a side (Bedrock 2000, Vertex 2576),
# and each view_image call re-sends every image the thread has viewed. A long
# visual-review thread crosses both lines and then fails on every later view.
# Images are downscaled to this edge when injected, and only the most recent
# ones are re-sent (``merge_viewed_images`` keeps the dict in view order).
# Both limits are tighter than Anthropic's: atlas-nicholas thread 60e3181f
# (2026-09-30) re-sent 24 screenshots (35 MP, ~37k vision tokens) on local-qwen,
# whose SGLang CPU image preprocessing then outlived the 600 s request timeout
# on every call, so each new user message timed out the same way. 1568 px is
# Anthropic's recommended long edge (larger images are downscaled server-side
# anyway), and four images cover "the one just viewed plus the few before it";
# older ones are listed and can be viewed again.
_MAX_IMAGE_EDGE_PX = 1568
_MAX_CONTEXT_IMAGES = 4

# [argus patch #97] A model call that carried image context and timed out is
# retried once without the images, so an oversized image context degrades to a
# text note instead of repeating the same timeout (and LLMErrorHandling's
# retries of it) on every later turn.
_TIMEOUT_ERROR_NAMES = frozenset({"APITimeoutError", "ReadTimeout", "WriteTimeout", "PoolTimeout", "TimeoutException", "StreamChunkTimeoutError", "TimeoutError"})


def _is_timeout(exc: BaseException) -> bool:
    """[argus patch #97] True when *exc*, or an exception it wraps, is a request timeout."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__name__ in _TIMEOUT_ERROR_NAMES:
            return True
        current = current.__cause__ or current.__context__
    return False


def _recent_viewed_images(viewed_images: dict) -> tuple[list[tuple[str, dict]], int]:
    """[argus patch #97] Return the most recently viewed images and how many were left out."""
    items = list(viewed_images.items())
    omitted = max(0, len(items) - _MAX_CONTEXT_IMAGES)
    return items[omitted:], omitted


def _fit_image_edge(data_url: str) -> tuple[str, tuple[int, int] | None]:
    """[argus patch #97] Downscale an image data URL so neither side exceeds ``_MAX_IMAGE_EDGE_PX``.

    Returns the data URL to send and, when it was downscaled, the original
    size. An image Pillow cannot read or re-encode passes through unchanged.
    """
    _, _, payload = data_url.partition(",")
    try:
        from PIL import Image

        with Image.open(io.BytesIO(base64.b64decode(payload))) as image:
            original_size = image.size
            if max(original_size) <= _MAX_IMAGE_EDGE_PX:
                return data_url, None
            resized = image.convert("RGBA") if image.mode in ("P", "PA") else image.copy()
        resized.thumbnail((_MAX_IMAGE_EDGE_PX, _MAX_IMAGE_EDGE_PX), Image.Resampling.LANCZOS)
        # Resampling defeats PNG compression (a 1600x2238 screenshot re-encodes
        # from 1.2 MB to 1.6 MB), so opaque images go out as JPEG (~0.36 MB);
        # only real transparency keeps PNG.
        transparent = resized.mode in ("RGBA", "LA") and resized.getchannel("A").getextrema()[0] < 255
        image_format = "PNG" if transparent else "JPEG"
        if not transparent and resized.mode not in ("RGB", "L"):
            resized = resized.convert("RGB")
        buffer = io.BytesIO()
        resized.save(buffer, format=image_format, **({} if transparent else {"quality": 85}))
    except Exception as exc:
        logger.warning("Could not downscale a viewed image, sending it unchanged: %s", exc)
        return data_url, None
    encoded = base64.b64encode(buffer.getvalue()).decode("utf-8")
    return f"data:image/{image_format.lower()};base64,{encoded}", original_size


class ViewImageMiddlewareState(ThreadState):
    """Reuse the thread state so reducer-backed keys keep their annotations."""


class ViewImageMiddleware(AgentMiddleware[ViewImageMiddlewareState]):
    """Injects image details into the model request when view_image tool calls have completed.

    This middleware:
    1. Wraps each LLM call
    2. Checks if the last assistant message contains view_image tool calls
    3. Verifies all tool calls in that message have been completed (have corresponding ToolMessages)
    4. If conditions are met, appends a human message with all viewed image details (including base64 data)
    5. Hands the augmented request to the model so it can see and analyze the images

    This enables the LLM to automatically receive and analyze images that were loaded via view_image tool,
    without requiring explicit user prompts to describe the images.

    Injection happens in ``wrap_model_call`` on purpose: the message exists only
    in ``ModelRequest.messages`` and is never returned as a state update, so no
    checkpoint carries the base64 payload and an interrupted run cannot strand it
    in history. Do not move this back to a ``before_model``/``after_model`` pair
    -- that writes the payload into state and can only take it out again
    afterwards (see #4267).
    """

    state_schema = ViewImageMiddlewareState

    # [argus patch #20] Render-verification-focused describe prompt: when the lead
    # model is NOT vision-capable, a vision model (e.g. local-qwen) describes the
    # screenshot and the TEXT is injected so the lead can act on visual defects it
    # cannot see.
    _DESCRIBE_PROMPT = (
        "Describe this rendered screenshot for a developer verifying their UI. "
        "State, specifically and literally: the overall layout, ALL visible text "
        "verbatim, the colors used, and ANY rendering problems you can see - blank "
        "or all-white areas, overlapping or cut-off elements, error messages, and "
        "missing or broken images. If it looks correct, say so plainly. Do not "
        "speculate about code; report only what is visible."
    )

    def __init__(self, *, vision_model_name: str | None = None, app_config=None) -> None:
        """[argus patch #20] ``vision_model_name``: when set, the LEAD model is
        non-vision, so route each viewed image through this vision-capable model
        for a TEXT description that is injected instead of the raw image. When
        None, the lead model is vision-capable and the image is injected directly
        (upstream behaviour)."""
        super().__init__()
        self._vision_model_name = vision_model_name
        self._app_config = app_config

    @staticmethod
    def _is_image_context_message(message: object) -> bool:
        """Return whether a message is trusted transient image context."""
        return isinstance(message, HumanMessage) and bool(message.id) and message.id.startswith(_IMAGE_CONTEXT_MESSAGE_ID_PREFIX) and message.additional_kwargs.get(_IMAGE_CONTEXT_MESSAGE_MARKER_KEY) is True

    def _get_last_assistant_message(self, messages: list) -> AIMessage | None:
        """Get the last assistant message from the message list.

        Args:
            messages: List of messages

        Returns:
            Last AIMessage or None if not found
        """
        for msg in reversed(messages):
            if isinstance(msg, AIMessage):
                return msg
        return None

    def _has_view_image_tool(self, message: AIMessage) -> bool:
        """Check if the assistant message contains view_image tool calls.

        Args:
            message: Assistant message to check

        Returns:
            True if message contains view_image tool calls
        """
        if not hasattr(message, "tool_calls") or not message.tool_calls:
            return False

        return any(tool_call.get("name") == "view_image" for tool_call in message.tool_calls)

    def _all_tools_completed(self, messages: list, assistant_msg: AIMessage) -> bool:
        """Check if all tool calls in the assistant message have been completed.

        Args:
            messages: List of all messages
            assistant_msg: The assistant message containing tool calls

        Returns:
            True if all tool calls have corresponding ToolMessages
        """
        if not hasattr(assistant_msg, "tool_calls") or not assistant_msg.tool_calls:
            return False

        # Get all tool call IDs from the assistant message
        tool_call_ids = {tool_call.get("id") for tool_call in assistant_msg.tool_calls if tool_call.get("id")}

        # Find the index of the assistant message
        try:
            assistant_idx = messages.index(assistant_msg)
        except ValueError:
            return False

        # Get all ToolMessages after the assistant message
        completed_tool_ids = set()
        for msg in messages[assistant_idx + 1 :]:
            if isinstance(msg, ToolMessage) and msg.tool_call_id:
                completed_tool_ids.add(msg.tool_call_id)

        # Check if all tool calls have been completed
        return tool_call_ids.issubset(completed_tool_ids)

    @staticmethod
    def _encode_image_bytes(
        image_bytes: bytes,
        mime_type: str,
        expected_size: int,
        expected_sha256: str | None = None,
    ) -> str | None:
        """Validate image bytes against recorded metadata and return a data URL."""
        current_size = len(image_bytes)
        if current_size != expected_size or current_size > _MAX_IMAGE_BYTES:
            return None
        if expected_sha256 is not None and hashlib.sha256(image_bytes).hexdigest() != expected_sha256:
            return None
        base64_data = base64.b64encode(image_bytes).decode("utf-8")
        return f"data:{mime_type};base64,{base64_data}"

    @classmethod
    def _read_host_image_as_data_url(
        cls,
        actual_path: str,
        mime_type: str,
        expected_size: int,
        expected_sha256: str | None = None,
    ) -> str | None:
        """Read a validated host mirror and return a data URL, or None on failure.

        Callers must first bind ``actual_path`` to the current run's user,
        thread, and virtual image path. The host path remains the compatibility
        path for local execution and older checkpoints. Provenance-aware
        checkpoints additionally verify the exact SHA-256 before a synchronized
        host copy can stand in for bytes from an earlier sandbox generation.
        """
        try:
            file_path = Path(actual_path)
            if not file_path.exists() or not file_path.is_file():
                return None
            current_size = file_path.stat().st_size
            if current_size != expected_size or current_size > _MAX_IMAGE_BYTES:
                return None
            with open(file_path, "rb") as f:
                image_bytes = f.read()
            return cls._encode_image_bytes(
                image_bytes,
                mime_type,
                expected_size,
                expected_sha256,
            )
        except OSError:
            return None

    @classmethod
    def _read_blob_image_as_data_url(
        cls,
        blob_ref_data: Mapping[str, object] | None,
        mime_type: str,
        expected_size: int,
        expected_sha256: str | None,
    ) -> str | None:
        """Resolve a trusted checkpoint blob ref, rejecting metadata drift."""
        if blob_ref_data is None or expected_sha256 is None:
            return None

        from deerflow.storage import BlobRef, get_blob_store_if_enabled

        try:
            ref = BlobRef.model_validate(dict(blob_ref_data))
        except (TypeError, ValueError):
            return None
        if ref.kind != "viewed-image" or ref.sha256 != expected_sha256 or ref.size != expected_size or ref.content_type != mime_type:
            return None

        try:
            store = get_blob_store_if_enabled()
            if store is None:
                return None
            image_bytes = store.get_bytes(ref)
        except Exception:
            logger.warning(
                "Failed to resolve viewed image blob %s",
                ref.sha256[:12],
                exc_info=True,
            )
            return None
        return cls._encode_image_bytes(
            image_bytes,
            mime_type,
            expected_size,
            expected_sha256,
        )

    @classmethod
    def _read_image_as_data_url(
        cls,
        state: ViewImageMiddlewareState,
        image_path: str,
        actual_path: str,
        mime_type: str,
        expected_size: int,
        expected_sha256: str | None,
        source_sandbox_id: str | None,
        blob_ref_data: Mapping[str, object] | None = None,
        *,
        allow_host_copy: bool = False,
    ) -> str | None:
        """Read the exact image bytes represented by ``viewed_images`` metadata.

        A live sandbox is authoritative only for metadata recorded from that same
        sandbox generation. If the thread now points at a replacement sandbox,
        the previous image can be reconstructed from the synchronized host mirror
        only when its SHA-256 exactly matches the bytes that ``view_image`` saw.
        Legacy metadata without a digest never authorizes this cross-generation
        fallback. When no live sandbox exists, the historical host compatibility
        path remains available (digest-checked when present).
        """
        blob_data_url = cls._read_blob_image_as_data_url(
            blob_ref_data,
            mime_type,
            expected_size,
            expected_sha256,
        )
        if blob_data_url is not None:
            return blob_data_url

        from deerflow.sandbox.overwrite import unwrap_sandbox
        from deerflow.sandbox.sandbox_provider import get_sandbox_provider

        sandbox_state, _ = unwrap_sandbox(state.get("sandbox"))
        sandbox_id = sandbox_state.get("sandbox_id") if isinstance(sandbox_state, dict) else None
        sandbox = get_sandbox_provider().get(sandbox_id) if sandbox_id else None

        if sandbox is not None:
            provenance_matches_live = source_sandbox_id == sandbox_id
            provenance_identifies_other_source = expected_sha256 is not None and source_sandbox_id != sandbox_id

            if provenance_identifies_other_source:
                # The current client belongs to a different generation (or the
                # image was originally read from the host). Reproduce the exact
                # historical bytes rather than letting an unrelated same-path
                # file in the replacement sandbox win.
                if actual_path and allow_host_copy:
                    host_data_url = cls._read_host_image_as_data_url(
                        actual_path,
                        mime_type,
                        expected_size,
                        expected_sha256,
                    )
                    if host_data_url is not None:
                        return host_data_url
                try:
                    image_bytes = sandbox.download_file(image_path)
                except Exception:
                    logger.warning(
                        "Failed to recover viewed image %s from replacement sandbox %s",
                        image_path,
                        sandbox_id,
                        exc_info=True,
                    )
                    return None
                return cls._encode_image_bytes(
                    image_bytes,
                    mime_type,
                    expected_size,
                    expected_sha256,
                )

            if not provenance_matches_live and expected_sha256 is None:
                # A legacy checkpoint cannot prove which sandbox generation
                # supplied these bytes. Do not silently reinterpret historical
                # image context through a newly active remote filesystem.
                return None

            try:
                image_bytes = sandbox.download_file(image_path)
            except Exception:
                logger.warning("Failed to read viewed image %s from sandbox %s", image_path, sandbox_id, exc_info=True)
                return None
            return cls._encode_image_bytes(
                image_bytes,
                mime_type,
                expected_size,
                expected_sha256,
            )

        if not actual_path or not allow_host_copy:
            return None
        return cls._read_host_image_as_data_url(
            actual_path,
            mime_type,
            expected_size,
            expected_sha256,
        )

    def _create_image_details_message(
        self,
        state: ViewImageMiddlewareState,
        *,
        host_path_allowed: Callable[[str, str], bool] | None = None,
    ) -> list[str | dict]:
        """Create a formatted message with all viewed image details.

        Reads image files on-demand from the active sandbox when available and
        encodes them as base64 for the model. The base64 data is NOT persisted in
        state -- only lightweight metadata (path, mime_type, size, digest, and
        source sandbox id when applicable) is stored in ``viewed_images``,
        avoiding large duplicate payloads across every checkpoint (see #4138).

        Args:
            state: Current state containing viewed_images

        Returns:
            List of content blocks (text and images) for the HumanMessage
        """
        viewed_images = state.get("viewed_images", {})
        if not viewed_images:
            # Return a properly formatted text block, not a plain string array
            return [{"type": "text", "text": "No images have been viewed."}]

        # Build the message with image information
        content_blocks: list[str | dict] = [{"type": "text", "text": "Here are the images you've viewed:"}]
        recent_images, omitted = _recent_viewed_images(viewed_images)
        if omitted:
            content_blocks.append({"type": "text", "text": f"({omitted} earlier viewed image(s) not re-sent; call view_image again to see one.)"})

        for image_path, image_data in recent_images:
            mime_type = image_data.get("mime_type", "unknown")
            actual_path = image_data.get("actual_path", "")
            expected_size = image_data.get("size", 0)
            expected_sha256 = image_data.get("sha256")
            source_sandbox_id = image_data.get("source_sandbox_id")
            blob_ref = image_data.get("blob_ref")

            # Read the image file on-demand and encode as base64 for the model
            data_url = self._read_image_as_data_url(
                state,
                image_path,
                actual_path,
                mime_type,
                expected_size,
                expected_sha256 if isinstance(expected_sha256, str) else None,
                source_sandbox_id if isinstance(source_sandbox_id, str) else None,
                blob_ref if isinstance(blob_ref, Mapping) else None,
                allow_host_copy=host_path_allowed(image_path, actual_path) if host_path_allowed is not None else False,
            )
            original_size = None
            if data_url:
                data_url, original_size = _fit_image_edge(data_url)

            # Add text description
            description = f"\n- **{image_path}** ({mime_type})"
            if original_size:
                description += f", downscaled from {original_size[0]}x{original_size[1]} to fit {_MAX_IMAGE_EDGE_PX} px; crop a region and view it for detail"
            content_blocks.append({"type": "text", "text": description})

            if data_url:
                content_blocks.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url},
                    }
                )
            else:
                content_blocks.append({"type": "text", "text": f"  (file unavailable or changed: {image_path})"})

        return content_blocks

    def _should_inject_image_message(self, messages: list[AnyMessage]) -> bool:
        """Determine if we should append an image details message.

        Args:
            messages: Messages about to be sent to the model

        Returns:
            True if we should append the message
        """
        if not messages:
            return False

        # Get the last assistant message
        last_assistant_msg = self._get_last_assistant_message(messages)
        if not last_assistant_msg:
            return False

        # Check if it has view_image tool calls
        if not self._has_view_image_tool(last_assistant_msg):
            return False

        # Check if all tools have been completed
        if not self._all_tools_completed(messages, last_assistant_msg):
            return False

        # Skip when image details are already present. ``_inject`` has stripped
        # this middleware's own messages by now, so what remains are unmarked
        # ones from checkpoints written before the marker existed -- those cannot
        # be told apart from user-authored text with certainty, so they are left
        # in place and simply not duplicated.
        assistant_idx = messages.index(last_assistant_msg)
        for msg in messages[assistant_idx + 1 :]:
            if isinstance(msg, HumanMessage):
                content_str = str(msg.content)
                if "Here are the images you've viewed" in content_str or "Here are the details of the images you've viewed" in content_str:
                    # Already added, don't add again
                    return False

        return True

    @staticmethod
    def _create_image_context_message(content: list[str | dict]) -> HumanMessage:
        """Create an identifiable, model-only image context message."""
        return HumanMessage(
            id=f"{_IMAGE_CONTEXT_MESSAGE_ID_PREFIX}{uuid4().hex}",
            content=content,
            additional_kwargs={
                "hide_from_ui": True,
                _IMAGE_CONTEXT_MESSAGE_MARKER_KEY: True,
                **provenance_kwargs(ContentKind.IMAGE_PAYLOAD, "view_image"),
            },
        )

    # ── [argus patch #20] vision-describe path for non-vision lead models ────

    async def _describe_image(self, path: Path, mime_type: str, b64_data: str) -> str:
        """Ask the configured vision model for a text description; best-effort."""
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

    def _describe_inputs(self, request: ModelRequest) -> list[tuple[str, str, str, str | None]]:
        """Blocking half of the describe path: read each viewed image's bytes.

        Uses the same provenance-checked reader (blob store, live sandbox,
        request-bound host copy) as the vision-lead path, so a replacement
        sandbox or a changed file is never described as if it were the image
        ``view_image`` saw. Returns ``(image_path, mime_type, actual_path,
        base64 | None)`` per viewed image.
        """
        state = request.state or {}
        inputs: list[tuple[str, str, str, str | None]] = []
        recent_images, _ = _recent_viewed_images(state.get("viewed_images", {}))
        for image_path, image_data in recent_images:
            mime_type = image_data.get("mime_type", "unknown")
            actual_path = image_data.get("actual_path", "")
            expected_size = image_data.get("size", 0)
            expected_sha256 = image_data.get("sha256")
            source_sandbox_id = image_data.get("source_sandbox_id")
            blob_ref = image_data.get("blob_ref")
            data_url = self._read_image_as_data_url(
                state,
                image_path,
                actual_path,
                mime_type,
                expected_size,
                expected_sha256 if isinstance(expected_sha256, str) else None,
                source_sandbox_id if isinstance(source_sandbox_id, str) else None,
                blob_ref if isinstance(blob_ref, Mapping) else None,
                allow_host_copy=self._host_path_matches_request(request, image_path, actual_path),
            )
            if data_url:
                data_url, _ = _fit_image_edge(data_url)
                mime_type = data_url.partition(";")[0].removeprefix("data:") or mime_type
            b64_data = data_url.split(",", 1)[1] if data_url and "," in data_url else None
            inputs.append((image_path, mime_type, actual_path, b64_data))
        return inputs

    async def _ainject_described(self, request: ModelRequest) -> ModelRequest:
        """Async counterpart of :meth:`_inject` that injects TEXT descriptions.

        Same sweep/dedup rules as ``_inject``; the payload handed to the model is
        a hidden HumanMessage of text blocks (one per viewed image) instead of
        ``image_url`` blocks, because the lead model cannot consume pixels.
        """
        messages = [message for message in request.messages if not self._is_image_context_message(message)]
        dropped_stranded = len(messages) != len(request.messages)
        if not self._should_inject_image_message(messages):
            return request.override(messages=messages) if dropped_stranded else request
        inputs = await run_sync_lifecycle_operation(self._describe_inputs, request)
        if not inputs:
            return request.override(messages=messages) if dropped_stranded else request
        text_blocks: list[str | dict] = []
        for image_path, mime_type, actual_path, b64_data in inputs:
            path = Path(actual_path or image_path)
            if b64_data is None:
                text_blocks.append({"type": "text", "text": f"**Visual description of `{path.name}`**: (file unavailable or changed: {image_path})"})
            else:
                text_blocks.append({"type": "text", "text": await self._describe_image(path, mime_type, b64_data)})
        logger.debug("Injecting vision-model descriptions of %d viewed image(s) into the model request", len(text_blocks))
        return request.override(messages=[*messages, self._create_image_context_message(text_blocks)])

    @staticmethod
    def _authorization_context(request: ModelRequest) -> Mapping:
        context = getattr(request.runtime, "context", None)
        return context if isinstance(context, Mapping) else {}

    @classmethod
    def _host_path_matches_request(cls, request: ModelRequest, image_path: str, actual_path: str) -> bool:
        """Bind a stored host copy to the authenticated run's user and thread."""
        from deerflow.config.paths import VIRTUAL_PATH_PREFIX, get_paths
        from deerflow.runtime.user_context import resolve_runtime_user_id
        from deerflow.sandbox.tools import resolve_and_validate_user_data_path, validate_local_tool_path
        from deerflow.tools.builtins.view_image_tool import _is_allowed_image_virtual_path

        if not isinstance(image_path, str) or not isinstance(actual_path, str) or not _is_allowed_image_virtual_path(image_path):
            return False

        from langgraph.config import get_config

        context_thread_id = cls._authorization_context(request).get("thread_id")
        try:
            configurable = get_config().get("configurable")
            configured_thread_id = configurable.get("thread_id") if isinstance(configurable, Mapping) else None
        except RuntimeError:
            configured_thread_id = None
        if configured_thread_id and context_thread_id and configured_thread_id != context_thread_id:
            return False
        thread_id = configured_thread_id or context_thread_id
        if not isinstance(thread_id, str) or not thread_id:
            return False

        try:
            user_id = resolve_runtime_user_id(request.runtime)
            thread_data = (request.state or {}).get("thread_data")
            if isinstance(thread_data, Mapping):
                # ThreadDataMiddleware may use a custom Paths base. Bind its
                # selected root to this run before using it to resolve a host copy.
                root_name = image_path.removeprefix(f"{VIRTUAL_PATH_PREFIX}/").split("/", 1)[0]
                root_path = thread_data.get(f"{root_name}_path")
                if not isinstance(root_path, str) or not root_path:
                    return False
                root = Path(root_path).resolve()
                if root.parts[-6:] != ("users", user_id, "threads", thread_id, "user-data", root_name):
                    return False
                validate_local_tool_path(image_path, thread_data, read_only=True)
                expected_path = Path(resolve_and_validate_user_data_path(image_path, thread_data))
            else:
                expected_path = get_paths().resolve_virtual_path(thread_id, image_path, user_id=user_id)
            return Path(actual_path).resolve() == expected_path
        except (OSError, PermissionError, TypeError, ValueError):
            return False

    def _image_injection_plan(self, request: ModelRequest) -> tuple[list[AnyMessage], bool, bool]:
        """Share message cleanup and read eligibility across sync/async paths."""
        messages = [message for message in request.messages if not self._is_image_context_message(message)]
        should_inject = self._should_inject_image_message(messages)
        needs_authorization = should_inject and bool((request.state or {}).get("viewed_images"))
        return messages, should_inject, needs_authorization

    def _inject(self, request: ModelRequest, *, authorization_checked: bool = False) -> ModelRequest:
        """Rebuild the request's image context from ``viewed_images``.

        Args:
            request: The pending model request
            authorization_checked: True only after ``awrap_model_call`` has
                enforced ``sandbox:execute`` for this exact request; skips the
                synchronous recheck inside its worker thread.

        Returns:
            A request whose messages carry exactly the image context this call
            warrants -- one freshly built message, or none -- leaving the
            original request untouched when there is nothing to change
        """
        # This middleware owns the image context and rebuilds it from
        # ``viewed_images`` on every call, so drop any copy already in the list.
        # A thread checkpointed by the earlier before_model/after_model pair can
        # carry one that reached state but was never removed (the run died during
        # the model call); left in place it would ride along in every later
        # request for the life of the thread. Matching requires both the reserved
        # ID prefix and the server-owned marker, and Gateway strips that marker
        # from client input, so this can never drop a user-authored message.
        messages, should_inject, needs_authorization = self._image_injection_plan(request)
        dropped_stranded = len(messages) != len(request.messages)
        if dropped_stranded:
            logger.debug("Dropping %d stranded image context message(s) from the model request", len(request.messages) - len(messages))

        if not should_inject:
            return request.override(messages=messages) if dropped_stranded else request

        if needs_authorization and not authorization_checked:
            from deerflow.authz.sandbox_authz import authorize_sandbox_execution, safe_app_config
            from deerflow.sandbox.exceptions import SandboxAuthorizationError

            try:
                authorize_sandbox_execution(context=self._authorization_context(request), app_config=safe_app_config())
            except SandboxAuthorizationError:
                # A restored view may predate a role/policy change. Never read
                # its host mirror or remote sandbox for an unauthorized request.
                return request.override(messages=messages) if dropped_stranded else request

        # Mixed content (text + images) for the model only, so hide it from the
        # chat UI and IM channels (matches the other middleware-injected context
        # messages) even though it never leaves this request.
        image_content = self._create_image_details_message(
            request.state or {},
            host_path_allowed=lambda image_path, actual_path: self._host_path_matches_request(request, image_path, actual_path),
        )
        logger.debug("Injecting image details message with images into the model request")

        return request.override(messages=[*messages, self._create_image_context_message(image_content)])

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        if self._vision_model_name:
            # [argus patch #20] The describe call is awaitable only; the sync path
            # sweeps stranded image context and defers injection to the async path.
            messages = [message for message in request.messages if not self._is_image_context_message(message)]
            if len(messages) != len(request.messages):
                return handler(request.override(messages=messages))
            return handler(request)
        # Sync injection executes inline on this call stack. There is no detached
        # worker to drain: an outer sandbox lease cannot reach its finally/release
        # boundary until this blocking read returns or raises.
        injected_request = self._inject(request)
        try:
            return handler(injected_request)
        except Exception as exc:
            fallback = self._without_images_after_timeout(injected_request, exc)
            if fallback is None:
                raise
            return handler(fallback)

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        # Image reads + base64 encoding can be slow (up to 20MB), so offload the
        # blocking work without allowing cancellation to outlive a sandbox
        # client operation. The outer run lease may release the client as soon as
        # cancellation propagates.
        messages, _, needs_authorization = self._image_injection_plan(request)
        if needs_authorization:
            from deerflow.authz.sandbox_authz import authorize_sandbox_execution_async, safe_app_config_async
            from deerflow.sandbox.exceptions import SandboxAuthorizationError

            try:
                await authorize_sandbox_execution_async(context=self._authorization_context(request), app_config=await safe_app_config_async())
            except SandboxAuthorizationError:
                # Still sweep any old model-only image context before handing
                # the request on; do not schedule a read in a worker.
                return await handler(request.override(messages=messages))

        if self._vision_model_name:
            # [argus patch #20] Non-vision lead: inject text descriptions instead.
            return await handler(await self._ainject_described(request))
        injected_request = await run_sync_lifecycle_operation(self._inject, request, authorization_checked=True)
        try:
            return await handler(injected_request)
        except Exception as exc:
            fallback = self._without_images_after_timeout(injected_request, exc)
            if fallback is None:
                raise
            return await handler(fallback)

    def _without_images_after_timeout(self, request: ModelRequest, exc: BaseException) -> ModelRequest | None:
        """[argus patch #97] The retry request for a timed-out call that carried images, else None.

        Only a timeout qualifies, and only when this middleware put image
        blocks into the request; the replacement carries a text note in place
        of the images so the model knows why it cannot see them.
        """
        if not _is_timeout(exc):
            return None
        image_count = 0
        kept: list = []
        for message in request.messages:
            if self._is_image_context_message(message):
                content = message.content if isinstance(message.content, list) else []
                image_count += sum(1 for block in content if isinstance(block, dict) and block.get("type") == "image_url")
            else:
                kept.append(message)
        if image_count == 0:
            return None
        logger.warning("Model call carrying %d viewed image(s) timed out; retrying once without them", image_count)
        note = f"({image_count} viewed image(s) were not sent: the request carrying them timed out. Continue from what you already know; to look again, view one image at a time or crop to the region you need.)"
        return request.override(messages=[*kept, self._create_image_context_message([{"type": "text", "text": note}])])

"""ChannelManager — consumes inbound messages and dispatches them to the DeerFlow agent via Gateway."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import httpx
from langgraph_sdk.errors import ConflictError

from app.channels.commands import KNOWN_CHANNEL_COMMANDS
from app.channels.message_bus import InboundMessage, InboundMessageType, MessageBus, OutboundMessage, ResolvedAttachment
from app.channels.store import ChannelStore
from app.gateway.csrf_middleware import CSRF_COOKIE_NAME, CSRF_HEADER_NAME, generate_csrf_token
from app.gateway.internal_auth import create_internal_auth_headers
from deerflow.runtime.user_context import get_effective_user_id

logger = logging.getLogger(__name__)

DEFAULT_LANGGRAPH_URL = "http://localhost:8001/api"
DEFAULT_GATEWAY_URL = "http://localhost:8001"
DEFAULT_ASSISTANT_ID = "lead_agent"
CUSTOM_AGENT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9-]+$")

DEFAULT_RUN_CONFIG: dict[str, Any] = {"recursion_limit": 100}
DEFAULT_RUN_CONTEXT: dict[str, Any] = {
    "thinking_enabled": True,
    "is_plan_mode": False,
    "subagent_enabled": False,
}
STREAM_UPDATE_MIN_INTERVAL_SECONDS = 0.35
THREAD_BUSY_MESSAGE = "This conversation is already processing another request. Please wait for it to finish and try again."

CHANNEL_CAPABILITIES = {
    "dingtalk": {"supports_streaming": False},
    "discord": {"supports_streaming": False},
    "feishu": {"supports_streaming": True},
    "slack": {"supports_streaming": False},
    # [argus patch #10] Telegram streams to drive the live stage-emoji
    # progress indicator. It does NOT receive streamed partial answer text
    # (suppressed in _handle_streaming_chat) — only stage signals + the final.
    "telegram": {"supports_streaming": True},
    "wechat": {"supports_streaming": False},
    "wecom": {"supports_streaming": True},
}

InboundFileReader = Callable[[dict[str, Any], httpx.AsyncClient], Awaitable[bytes | None]]

_METADATA_DROP_KEYS = frozenset({"raw_message", "ref_msg"})


def _slim_metadata(meta: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of *meta* with known-large keys removed."""
    return {k: v for k, v in meta.items() if k not in _METADATA_DROP_KEYS}


INBOUND_FILE_READERS: dict[str, InboundFileReader] = {}


def register_inbound_file_reader(channel_name: str, reader: InboundFileReader) -> None:
    INBOUND_FILE_READERS[channel_name] = reader


async def _read_http_inbound_file(file_info: dict[str, Any], client: httpx.AsyncClient) -> bytes | None:
    url = file_info.get("url")
    if not isinstance(url, str) or not url:
        return None

    resp = await client.get(url)
    resp.raise_for_status()
    return resp.content


async def _read_wecom_inbound_file(file_info: dict[str, Any], client: httpx.AsyncClient) -> bytes | None:
    data = await _read_http_inbound_file(file_info, client)
    if data is None:
        return None

    aeskey = file_info.get("aeskey") if isinstance(file_info.get("aeskey"), str) else None
    if not aeskey:
        return data

    try:
        from aibot.crypto_utils import decrypt_file
    except Exception:
        logger.exception("[Manager] failed to import WeCom decrypt_file")
        return None

    return decrypt_file(data, aeskey)


async def _read_wechat_inbound_file(file_info: dict[str, Any], client: httpx.AsyncClient) -> bytes | None:
    raw_path = file_info.get("path")
    if isinstance(raw_path, str) and raw_path.strip():
        try:
            return await asyncio.to_thread(Path(raw_path).read_bytes)
        except OSError:
            logger.exception("[Manager] failed to read WeChat inbound file from local path: %s", raw_path)
            return None

    full_url = file_info.get("full_url")
    if isinstance(full_url, str) and full_url.strip():
        return await _read_http_inbound_file({"url": full_url}, client)

    return None


register_inbound_file_reader("wecom", _read_wecom_inbound_file)
register_inbound_file_reader("wechat", _read_wechat_inbound_file)


class InvalidChannelSessionConfigError(ValueError):
    """Raised when IM channel session overrides contain invalid agent config."""


def _is_thread_busy_error(exc: BaseException | None) -> bool:
    if exc is None:
        return False
    if isinstance(exc, ConflictError):
        return True
    return "already running a task" in str(exc)


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _merge_dicts(*layers: Any) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for layer in layers:
        if isinstance(layer, Mapping):
            merged.update(layer)
    return merged


def _normalize_custom_agent_name(raw_value: str) -> str:
    """Normalize legacy channel assistant IDs into valid custom agent names."""
    normalized = raw_value.strip().lower().replace("_", "-")
    if not normalized:
        raise InvalidChannelSessionConfigError("Channel session assistant_id is empty. Use 'lead_agent' or a valid custom agent name.")
    if not CUSTOM_AGENT_NAME_PATTERN.fullmatch(normalized):
        raise InvalidChannelSessionConfigError(f"Invalid channel session assistant_id {raw_value!r}. Use 'lead_agent' or a custom agent name containing only letters, digits, and hyphens.")
    return normalized


def _extract_response_text(result: dict | list) -> str:
    """Extract the last AI message text from a LangGraph runs.wait result.

    ``runs.wait`` returns the final state dict which contains a ``messages``
    list.  Each message is a dict with at least ``type`` and ``content``.

    Handles special cases:
    - Regular AI text responses
    - Clarification interrupts (``ask_clarification`` tool messages)
    """
    if isinstance(result, list):
        messages = result
    elif isinstance(result, dict):
        messages = result.get("messages", [])
    else:
        return ""

    # Walk backwards to find usable response text, but stop at the last
    # human message to avoid returning text from a previous turn.
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue

        msg_type = msg.get("type")

        # Stop at the last human message — anything before it is a previous turn
        if msg_type == "human":
            break

        # Check for tool messages from ask_clarification (interrupt case)
        if msg_type == "tool" and msg.get("name") == "ask_clarification":
            content = msg.get("content", "")
            if isinstance(content, str) and content:
                return content

        # Regular AI message with text content
        if msg_type == "ai":
            content = msg.get("content", "")
            if isinstance(content, str) and content:
                return content
            # content can be a list of content blocks
            if isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif isinstance(block, str):
                        parts.append(block)
                text = "".join(parts)
                if text:
                    return text
    return ""


def _extract_text_content(content: Any) -> str:
    """Extract text from a streaming payload content field."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, Mapping):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
                else:
                    nested = block.get("content")
                    if isinstance(nested, str):
                        parts.append(nested)
        return "".join(parts)
    if isinstance(content, Mapping):
        for key in ("text", "content"):
            value = content.get(key)
            if isinstance(value, str):
                return value
    return ""


def _merge_stream_text(existing: str, chunk: str) -> str:
    """Merge either delta text or cumulative text into a single snapshot."""
    if not chunk:
        return existing
    if not existing or chunk == existing:
        return chunk or existing
    if chunk.startswith(existing):
        return chunk
    if existing.endswith(chunk):
        return existing
    return existing + chunk


def _extract_stream_message_id(payload: Any, metadata: Any) -> str | None:
    """Best-effort extraction of the streamed AI message identifier."""
    candidates = [payload, metadata]
    if isinstance(payload, Mapping):
        candidates.append(payload.get("kwargs"))

    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        for key in ("id", "message_id"):
            value = candidate.get(key)
            if isinstance(value, str) and value:
                return value
    return None


# [argus patch #10] Tool-name → coarse stage. Search/web tools surface as
# "searching"; everything else the agent invokes is "working".
_SEARCH_TOOL_HINTS = ("search", "web", "tavily", "exa", "browse", "fetch_url")


def _stage_from_chunk(event: str, data: Any) -> str | None:
    """Derive a coarse execution stage from one langgraph stream chunk, for the
    live progress indicator. Returns one of received/thinking/planning/
    searching/working, or None if the chunk implies no stage change.

    - messages-tuple AI chunk with tool_calls → planning (write_todos) /
      searching (search-ish tool) / working (any other tool).
    - messages-tuple AI chunk with text, no tool_calls → thinking.
    - values dict that contains a non-empty todos list → planning.
    """
    if event == "values":
        if isinstance(data, Mapping):
            todos = data.get("todos")
            if isinstance(todos, list) and todos:
                return "planning"
        return None

    # This langgraph runtime serves the "messages" stream event (the SDK
    # downgrades the requested "messages-tuple"); accept both. The payload is
    # the message chunk, sometimes wrapped as (chunk, metadata).
    if event not in ("messages", "messages-tuple"):
        return None

    payload = data
    if isinstance(data, (list, tuple)) and data:
        payload = data[0]
    if not isinstance(payload, Mapping):
        return None

    # Streamed AIMessageChunks carry tool calls as `tool_call_chunks` (the
    # incremental form) — `tool_calls` is only populated AFTER aggregation
    # (values mode), so reading only `tool_calls` here meant searching/working/
    # planning never fired over a streaming channel and the indicator was stuck
    # at thinking. Read all three shapes: tool_calls, tool_call_chunks, and
    # additional_kwargs.tool_calls. (A later delta of a chunk may carry an empty
    # name — we only need the first, which names the tool; we emit on change.)
    tool_calls = list(payload.get("tool_calls") or [])
    tool_calls += list(payload.get("tool_call_chunks") or [])
    ak = payload.get("additional_kwargs")
    if isinstance(ak, Mapping):
        tool_calls += list(ak.get("tool_calls") or [])
    if tool_calls:
        names = []
        for tc in tool_calls:
            if isinstance(tc, Mapping):
                # tool_calls: {"name": ...}; additional_kwargs form: {"function": {"name": ...}}
                nm = tc.get("name") or ((tc.get("function") or {}).get("name") if isinstance(tc.get("function"), Mapping) else "")
                if nm:
                    names.append(str(nm).lower())
        if names:
            if any(n == "write_todos" for n in names):
                return "planning"
            if any(any(h in n for h in _SEARCH_TOOL_HINTS) for n in names):
                return "searching"
            return "working"

    payload_type = str(payload.get("type", "")).lower()
    if "tool" in payload_type:
        return None  # a tool *result* coming back; the stage was set on the call
    if _extract_text_content(payload.get("content")):
        return "thinking"
    return None


def _accumulate_stream_text(
    buffers: dict[str, str],
    current_message_id: str | None,
    event_data: Any,
) -> tuple[str | None, str | None]:
    """Convert a ``messages-tuple`` event into the latest displayable AI text."""
    payload = event_data
    metadata: Any = None
    if isinstance(event_data, (list, tuple)):
        if event_data:
            payload = event_data[0]
        if len(event_data) > 1:
            metadata = event_data[1]

    if isinstance(payload, str):
        message_id = current_message_id or "__default__"
        buffers[message_id] = _merge_stream_text(buffers.get(message_id, ""), payload)
        return buffers[message_id], message_id

    if not isinstance(payload, Mapping):
        return None, current_message_id

    payload_type = str(payload.get("type", "")).lower()
    if "tool" in payload_type:
        return None, current_message_id

    text = _extract_text_content(payload.get("content"))
    if not text and isinstance(payload.get("kwargs"), Mapping):
        text = _extract_text_content(payload["kwargs"].get("content"))
    if not text:
        return None, current_message_id

    message_id = _extract_stream_message_id(payload, metadata) or current_message_id or "__default__"
    buffers[message_id] = _merge_stream_text(buffers.get(message_id, ""), text)
    return buffers[message_id], message_id


def _extract_artifacts(result: dict | list) -> list[str]:
    """Extract artifact paths from the last AI response cycle only.

    Instead of reading the full accumulated ``artifacts`` state (which contains
    all artifacts ever produced in the thread), this inspects the messages after
    the last human message and collects file paths from ``present_files`` tool
    calls.  This ensures only newly-produced artifacts are returned.
    """
    if isinstance(result, list):
        messages = result
    elif isinstance(result, dict):
        messages = result.get("messages", [])
    else:
        return []

    artifacts: list[str] = []
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        # Stop at the last human message — anything before it is a previous turn
        if msg.get("type") == "human":
            break
        # Look for AI messages with present_files tool calls
        if msg.get("type") == "ai":
            for tc in msg.get("tool_calls", []):
                if isinstance(tc, dict) and tc.get("name") == "present_files":
                    args = tc.get("args", {})
                    paths = args.get("filepaths", [])
                    if isinstance(paths, list):
                        artifacts.extend(p for p in paths if isinstance(p, str))
    return artifacts


def _format_artifact_text(artifacts: list[str]) -> str:
    """Format artifact paths into a human-readable text block listing filenames."""
    import posixpath

    filenames = [posixpath.basename(p) for p in artifacts]
    if len(filenames) == 1:
        return f"Created File: 📎 {filenames[0]}"
    return "Created Files: 📎 " + "、".join(filenames)


_OUTPUTS_VIRTUAL_PREFIX = "/mnt/user-data/outputs/"


def _resolve_attachments(thread_id: str, artifacts: list[str]) -> list[ResolvedAttachment]:
    """Resolve virtual artifact paths to host filesystem paths with metadata.

    Only paths under ``/mnt/user-data/outputs/`` are accepted; any other
    virtual path is rejected with a warning to prevent exfiltrating uploads
    or workspace files via IM channels.

    Skips artifacts that cannot be resolved (missing files, invalid paths)
    and logs warnings for them.
    """
    from deerflow.config.paths import get_paths

    attachments: list[ResolvedAttachment] = []
    paths = get_paths()
    user_id = get_effective_user_id()
    outputs_dir = paths.sandbox_outputs_dir(thread_id, user_id=user_id).resolve()
    for virtual_path in artifacts:
        # Security: only allow files from the agent outputs directory
        if not virtual_path.startswith(_OUTPUTS_VIRTUAL_PREFIX):
            logger.warning("[Manager] rejected non-outputs artifact path: %s", virtual_path)
            continue
        try:
            actual = paths.resolve_virtual_path(thread_id, virtual_path, user_id=user_id)
            # Verify the resolved path is actually under the outputs directory
            # (guards against path-traversal even after prefix check)
            try:
                actual.resolve().relative_to(outputs_dir)
            except ValueError:
                logger.warning("[Manager] artifact path escapes outputs dir: %s -> %s", virtual_path, actual)
                continue
            if not actual.is_file():
                logger.warning("[Manager] artifact not found on disk: %s -> %s", virtual_path, actual)
                continue
            mime, _ = mimetypes.guess_type(str(actual))
            mime = mime or "application/octet-stream"
            attachments.append(
                ResolvedAttachment(
                    virtual_path=virtual_path,
                    actual_path=actual,
                    filename=actual.name,
                    mime_type=mime,
                    size=actual.stat().st_size,
                    is_image=mime.startswith("image/"),
                )
            )
        except (ValueError, OSError) as exc:
            logger.warning("[Manager] failed to resolve artifact %s: %s", virtual_path, exc)
    return attachments


# [argus patch #10] Only these extensions are auto-presented when the agent
# writes a file to outputs/ but forgets to present_files it. The rule:
# auto-present VIEWABLE END-PRODUCTS only (a report, a diagram, an image), and
# NEVER the means used to produce them (a .py fetch script, a .json blob, a
# scratch .csv/.txt/.log). E.g. asking for the weather, where Qwen writes a
# throwaway fetch script into outputs/, must NOT yield a "/f/fetch.py" link.
# The agent can still explicitly present ANYTHING via present_files — this
# allowlist only gates the *automatic* rescue path.
_ORPHAN_PRESENT_EXTS = {".html", ".htm", ".svg", ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp"}


def _orphan_artifacts(thread_id: str, since: float, already: list[str]) -> list[str]:
    """[argus patch #10] Viewable deliverables the agent wrote to outputs/
    during this run but did NOT present via present_files. Models sometimes
    write a file and paste its contents into chat instead (observed with SVG);
    we rescue those so they get a /f/ link rather than a wall of source.

    Deliberately conservative — only the viewable-end-product extensions in
    _ORPHAN_PRESENT_EXTS qualify. Code/data/scratch files (.py, .json, .csv,
    .txt, .log, …) are ignored: they're the means, not the answer. Also skips
    already-presented files and render-and-verify *.screenshot.png sidecars."""
    try:
        from deerflow.config.paths import get_paths

        outputs_dir = get_paths().sandbox_outputs_dir(thread_id, user_id=get_effective_user_id()).resolve()
    except Exception:
        return []
    if not outputs_dir.is_dir():
        return []
    already_names = {p.rsplit("/", 1)[-1] for p in already}
    found: list[str] = []
    for f in sorted(outputs_dir.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0):
        try:
            if not f.is_file() or f.stat().st_mtime < since - 1:
                continue
        except OSError:
            continue
        if f.name in already_names or f.name.endswith(".screenshot.png"):
            continue
        if f.suffix.lower() not in _ORPHAN_PRESENT_EXTS:
            continue  # not a viewable end-product — skip (e.g. a fetch .py)
        found.append(_OUTPUTS_VIRTUAL_PREFIX + f.name)
    return found


def _prepare_artifact_delivery(
    thread_id: str,
    response_text: str,
    artifacts: list[str],
    channel_name: str | None = None,
    run_start: float | None = None,
) -> tuple[str, list[ResolvedAttachment]]:
    """Resolve attachments and append filename fallbacks to the text response.

    [argus patch #10] When the target channel is Telegram, hand off to the
    channel-aware presenter (_artifact_presenter.present_artifacts): it turns
    web-viewable artifacts (HTML/SVG) into VIEWABLE links to the per-stack
    /f/ fileserver instead of attaching the raw file, and links+attaches other
    binaries. It also folds in orphan artifacts (files written this run but not
    present_files'd) and strips the matching giant code block from the chat
    text. For every other channel the original behavior is unchanged.
    """
    attachments: list[ResolvedAttachment] = []

    # [argus] Channel-aware presentation for Telegram.
    if channel_name == "telegram":
        from app.channels._artifact_presenter import present_artifacts, strip_inlined_artifacts

        # Fold in files the agent wrote this run but forgot to present.
        if run_start is not None:
            for orphan in _orphan_artifacts(thread_id, run_start, artifacts):
                if orphan not in artifacts:
                    artifacts.append(orphan)
        if not artifacts:
            return response_text, attachments
        attachments = _resolve_attachments(thread_id, artifacts)
        # Drop oversized fenced/<pre> blocks whose content is one of the files
        # we're about to link — no point pasting an SVG AND linking it.
        response_text = strip_inlined_artifacts(response_text, attachments)
        block, attachments = present_artifacts(channel_name, thread_id, artifacts, attachments)
        if block:
            response_text = (response_text + "\n\n" + block) if response_text else block
        return response_text, attachments

    if not artifacts:
        return response_text, attachments

    attachments = _resolve_attachments(thread_id, artifacts)

    resolved_virtuals = {attachment.virtual_path for attachment in attachments}
    unresolved = [path for path in artifacts if path not in resolved_virtuals]

    if unresolved:
        artifact_text = _format_artifact_text(unresolved)
        response_text = (response_text + "\n\n" + artifact_text) if response_text else artifact_text

    # Always include resolved attachment filenames as a text fallback so files
    # remain discoverable even when the upload is skipped or fails.
    if attachments:
        resolved_text = _format_artifact_text([attachment.virtual_path for attachment in attachments])
        response_text = (response_text + "\n\n" + resolved_text) if response_text else resolved_text

    return response_text, attachments


async def _ingest_inbound_files(thread_id: str, msg: InboundMessage) -> list[dict[str, Any]]:
    if not msg.files:
        return []

    from deerflow.uploads.manager import (
        UnsafeUploadPathError,
        claim_unique_filename,
        ensure_uploads_dir,
        normalize_filename,
        write_upload_file_no_symlink,
    )

    uploads_dir = ensure_uploads_dir(thread_id)
    seen_names = {entry.name for entry in uploads_dir.iterdir() if entry.is_file()}

    created: list[dict[str, Any]] = []
    file_reader = INBOUND_FILE_READERS.get(msg.channel_name, _read_http_inbound_file)
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as client:
        for idx, f in enumerate(msg.files):
            if not isinstance(f, dict):
                continue

            ftype = f.get("type") if isinstance(f.get("type"), str) else "file"
            filename = f.get("filename") if isinstance(f.get("filename"), str) else ""

            try:
                data = await file_reader(f, client)
            except Exception:
                logger.exception(
                    "[Manager] failed to read inbound file: channel=%s, file=%s",
                    msg.channel_name,
                    f.get("url") or filename or idx,
                )
                continue

            if data is None:
                logger.warning(
                    "[Manager] inbound file reader returned no data: channel=%s, file=%s",
                    msg.channel_name,
                    f.get("url") or filename or idx,
                )
                continue

            if not filename:
                ext = ".bin"
                if ftype == "image":
                    ext = ".png"
                filename = f"{msg.thread_ts or 'msg'}_{idx}{ext}"

            try:
                safe_name = claim_unique_filename(normalize_filename(filename), seen_names)
            except ValueError:
                logger.warning(
                    "[Manager] skipping inbound file with unsafe filename: channel=%s, file=%r",
                    msg.channel_name,
                    filename,
                )
                continue

            dest = uploads_dir / safe_name
            try:
                dest = write_upload_file_no_symlink(uploads_dir, safe_name, data)
            except UnsafeUploadPathError:
                logger.warning("[Manager] skipping inbound file with unsafe destination: %s", safe_name)
                continue
            except Exception:
                logger.exception("[Manager] failed to write inbound file: %s", dest)
                continue

            created.append(
                {
                    "filename": safe_name,
                    "size": len(data),
                    "path": f"/mnt/user-data/uploads/{safe_name}",
                    "is_image": ftype == "image",
                }
            )

    return created


def _format_uploaded_files_block(files: list[dict[str, Any]]) -> str:
    lines = [
        "<uploaded_files>",
        "The following files were uploaded in this message:",
        "",
    ]
    if not files:
        lines.append("(empty)")
    else:
        for f in files:
            filename = f.get("filename", "")
            size = int(f.get("size") or 0)
            size_kb = size / 1024 if size else 0
            size_str = f"{size_kb:.1f} KB" if size_kb < 1024 else f"{size_kb / 1024:.1f} MB"
            path = f.get("path", "")
            is_image = bool(f.get("is_image"))
            file_kind = "image" if is_image else "file"
            lines.append(f"- {filename} ({size_str})")
            lines.append(f"  Type: {file_kind}")
            lines.append(f"  Path: {path}")
            lines.append("")
    lines.append("Use `read_file` for text-based files and documents.")
    lines.append("Use `view_image` for image files (jpg, jpeg, png, webp) so the model can inspect the image content.")
    lines.append("</uploaded_files>")
    return "\n".join(lines)


class ChannelManager:
    """Core dispatcher that bridges IM channels to the DeerFlow agent.

    It reads from the MessageBus inbound queue, creates/reuses threads on
    Gateway's LangGraph-compatible API, sends messages via ``runs.wait``, and publishes
    outbound responses back through the bus.
    """

    def __init__(
        self,
        bus: MessageBus,
        store: ChannelStore,
        *,
        max_concurrency: int = 5,
        langgraph_url: str = DEFAULT_LANGGRAPH_URL,
        gateway_url: str = DEFAULT_GATEWAY_URL,
        assistant_id: str = DEFAULT_ASSISTANT_ID,
        default_session: dict[str, Any] | None = None,
        channel_sessions: dict[str, Any] | None = None,
        coalesce_window: float | None = None,
    ) -> None:
        self.bus = bus
        self.store = store
        self._max_concurrency = max_concurrency
        # [argus patch #10] Debounced coalescing of split-paste CHAT messages.
        self._coalesce_window = coalesce_window
        self._coalescer = None  # built in start() once the loop exists
        self._langgraph_url = langgraph_url
        self._gateway_url = gateway_url
        self._assistant_id = assistant_id
        self._default_session = _as_dict(default_session)
        self._channel_sessions = dict(channel_sessions or {})
        self._client = None  # lazy init — langgraph_sdk async client
        self._csrf_token = generate_csrf_token()
        self._semaphore: asyncio.Semaphore | None = None
        self._running = False
        self._task: asyncio.Task | None = None

    @staticmethod
    def _channel_supports_streaming(channel_name: str) -> bool:
        from .service import get_channel_service

        service = get_channel_service()
        if service:
            channel = service.get_channel(channel_name)
            if channel is not None:
                return channel.supports_streaming
        return CHANNEL_CAPABILITIES.get(channel_name, {}).get("supports_streaming", False)

    def _resolve_session_layer(self, msg: InboundMessage) -> tuple[dict[str, Any], dict[str, Any]]:
        channel_layer = _as_dict(self._channel_sessions.get(msg.channel_name))
        users_layer = _as_dict(channel_layer.get("users"))
        user_layer = _as_dict(users_layer.get(msg.user_id))
        return channel_layer, user_layer

    def _resolve_run_params(self, msg: InboundMessage, thread_id: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
        channel_layer, user_layer = self._resolve_session_layer(msg)

        assistant_id = user_layer.get("assistant_id") or channel_layer.get("assistant_id") or self._default_session.get("assistant_id") or self._assistant_id
        if not isinstance(assistant_id, str) or not assistant_id.strip():
            assistant_id = self._assistant_id

        run_config = _merge_dicts(
            DEFAULT_RUN_CONFIG,
            self._default_session.get("config"),
            channel_layer.get("config"),
            user_layer.get("config"),
        )

        configurable = run_config.get("configurable")
        if isinstance(configurable, Mapping):
            configurable = dict(configurable)
        else:
            configurable = {}
        run_config["configurable"] = configurable
        # Pin channel-triggered runs to the root graph namespace so follow-up
        # turns continue from the same conversation checkpoint.
        configurable["checkpoint_ns"] = ""
        configurable["thread_id"] = thread_id

        run_context = _merge_dicts(
            DEFAULT_RUN_CONTEXT,
            self._default_session.get("context"),
            channel_layer.get("context"),
            user_layer.get("context"),
            {"thread_id": thread_id},
        )

        # Custom agents are implemented as lead_agent + agent_name context.
        # Keep backward compatibility for channel configs that set
        # assistant_id: <custom-agent-name> by routing through lead_agent.
        if assistant_id != DEFAULT_ASSISTANT_ID:
            run_context.setdefault("agent_name", _normalize_custom_agent_name(assistant_id))
            assistant_id = DEFAULT_ASSISTANT_ID

        return assistant_id, run_config, run_context

    # -- LangGraph SDK client (lazy) ----------------------------------------

    def _get_client(self):
        """Return the ``langgraph_sdk`` async client, creating it on first use."""
        if self._client is None:
            from langgraph_sdk import get_client

            self._client = get_client(
                url=self._langgraph_url,
                headers={
                    **create_internal_auth_headers(),
                    CSRF_HEADER_NAME: self._csrf_token,
                    "Cookie": f"{CSRF_COOKIE_NAME}={self._csrf_token}",
                },
            )
        return self._client

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Start the dispatch loop."""
        if self._running:
            return
        self._running = True
        self._semaphore = asyncio.Semaphore(self._max_concurrency)
        # [argus patch #10] Build the coalescer now that we're on the loop.
        from app.channels._coalesce import DEFAULT_COALESCE_WINDOW, MessageCoalescer

        window = self._coalesce_window if self._coalesce_window is not None else DEFAULT_COALESCE_WINDOW
        # window <= 0 disables coalescing (each message dispatches immediately) —
        # used by tests and any caller that wants the legacy 1-message-1-turn path.
        self._coalescer = MessageCoalescer(self._dispatch_handle, window=window) if window > 0 else None
        self._task = asyncio.create_task(self._dispatch_loop())
        logger.info("ChannelManager started (max_concurrency=%d, coalesce_window=%.1fs)", self._max_concurrency, window)

    async def stop(self) -> None:
        """Stop the dispatch loop."""
        self._running = False
        # [argus patch #10] Don't lose a buffered burst on shutdown.
        if self._coalescer is not None:
            try:
                await self._coalescer.flush()
            except Exception:
                logger.exception("[Manager] coalescer flush on stop failed")
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("ChannelManager stopped")

    # -- dispatch loop -----------------------------------------------------

    async def _dispatch_loop(self) -> None:
        logger.info("[Manager] dispatch loop started, waiting for inbound messages")
        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.get_inbound(), timeout=1.0)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            logger.info(
                "[Manager] received inbound: channel=%s, chat_id=%s, type=%s, text=%r",
                msg.channel_name,
                msg.chat_id,
                msg.msg_type.value,
                msg.text[:100] if msg.text else "",
            )
            # [argus patch #10] Coalesce CHAT messages per conversation so a
            # split paste (Telegram chunks a long message into several) becomes
            # ONE agent turn instead of racing on the same thread (the 2nd+
            # would 409 "thread busy" and be lost). Commands bypass coalescing
            # — a /new must run immediately and never merge.
            if self._coalescer is not None and msg.msg_type == InboundMessageType.CHAT:
                self._coalescer.add(msg)
            else:
                # Commands, or coalescing disabled (window<=0) → dispatch now.
                await self._dispatch_handle(msg)

    async def _dispatch_handle(self, msg: InboundMessage) -> None:
        """Spawn the actual message handler (used directly for commands and as
        the coalescer's dispatch callback for combined chat turns). Async so the
        coalescer can `await` it; it returns as soon as the task is scheduled."""
        task = asyncio.create_task(self._handle_message(msg))
        task.add_done_callback(self._log_task_error)

    @staticmethod
    def _log_task_error(task: asyncio.Task) -> None:
        """Surface unhandled exceptions from background tasks."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error("[Manager] unhandled error in message task: %s", exc, exc_info=exc)

    async def _handle_message(self, msg: InboundMessage) -> None:
        async with self._semaphore:
            try:
                if msg.msg_type == InboundMessageType.COMMAND:
                    await self._handle_command(msg)
                else:
                    # [argus patch] Surface the channel sender into run_context so
                    # tools can attribute an action to the requesting human (e.g.
                    # the Pythia correct_minutes tool stamps the commit author
                    # from channel_user_id). DeerFlow otherwise only uses
                    # msg.user_id for the thread store; no tool ever sees it.
                    # Channel-agnostic; merged into run_context in _handle_chat.
                    await self._handle_chat(msg, extra_context={
                        "channel_user_id": msg.user_id,
                        "channel_name": msg.channel_name,
                        "channel_id": msg.chat_id,
                        "thread_ts": msg.thread_ts,
                    })
            except InvalidChannelSessionConfigError as exc:
                logger.warning(
                    "Invalid channel session config for %s (chat=%s): %s",
                    msg.channel_name,
                    msg.chat_id,
                    exc,
                )
                await self._send_error(msg, str(exc))
            except Exception:
                logger.exception(
                    "Error handling message from %s (chat=%s)",
                    msg.channel_name,
                    msg.chat_id,
                )
                await self._send_error(msg, "An internal error occurred. Please try again.")

    # -- chat handling -----------------------------------------------------

    async def _create_thread(self, client, msg: InboundMessage) -> str:
        """Create a new thread through Gateway and store the mapping."""
        thread = await client.threads.create()
        thread_id = thread["thread_id"]
        self.store.set_thread_id(
            msg.channel_name,
            msg.chat_id,
            thread_id,
            topic_id=msg.topic_id,
            user_id=msg.user_id,
        )
        logger.info("[Manager] new thread created through Gateway: thread_id=%s for chat_id=%s topic_id=%s", thread_id, msg.chat_id, msg.topic_id)
        return thread_id

    async def _handle_chat(self, msg: InboundMessage, extra_context: dict[str, Any] | None = None) -> None:
        client = self._get_client()

        # Look up existing DeerFlow thread.
        # topic_id may be None (e.g. Telegram private chats) — the store
        # handles this by using the "channel:chat_id" key without a topic suffix.
        thread_id = self.store.get_thread_id(msg.channel_name, msg.chat_id, topic_id=msg.topic_id)
        if thread_id:
            logger.info("[Manager] reusing thread: thread_id=%s for topic_id=%s", thread_id, msg.topic_id)

        # No existing thread found — create a new one
        if thread_id is None:
            thread_id = await self._create_thread(client, msg)
            # [argus patch] This topic has no DeerFlow conversation yet. If it is
            # a reply under an existing thread (e.g. the raw-posted minutes
            # draft, which never created an agent thread), pull the thread's
            # earlier messages so the reply has the context it refers to.
            # Best-effort + only fetches when the channel supports it.
            try:
                from .service import get_channel_service
                channel = (get_channel_service() or None) and \
                    get_channel_service().get_channel(msg.channel_name)
                fetch = getattr(channel, "fetch_thread_context", None) if channel else None
                if fetch and msg.topic_id:
                    ctx = await fetch(msg.chat_id, msg.topic_id,
                                      (msg.metadata or {}).get("event_ts", ""))
                    if ctx:
                        msg.text = f"{ctx}\n\n{msg.text}".strip()
                        logger.info("[Manager] prepended thread context "
                                    "(%d chars) for new topic_id=%s",
                                    len(ctx), msg.topic_id)
            except Exception:  # noqa: BLE001 — context is best-effort
                logger.exception("[Manager] thread-context fetch failed")

        assistant_id, run_config, run_context = self._resolve_run_params(msg, thread_id)

        # If the inbound message contains file attachments, let the channel
        # materialize (download) them and update msg.text to include sandbox file paths.
        # This enables downstream models to access user-uploaded files by path.
        # Channels that do not support file download will simply return the original message.
        if msg.files:
            from .service import get_channel_service

            service = get_channel_service()
            channel = service.get_channel(msg.channel_name) if service else None
            logger.info("[Manager] preparing receive file context for %d attachments", len(msg.files))
            msg = await channel.receive_file(msg, thread_id) if channel else msg
        if extra_context:
            run_context.update(extra_context)

        uploaded = await _ingest_inbound_files(thread_id, msg)
        if uploaded:
            msg.text = f"{_format_uploaded_files_block(uploaded)}\n\n{msg.text}".strip()

        if self._channel_supports_streaming(msg.channel_name):
            await self._handle_streaming_chat(
                client,
                msg,
                thread_id,
                assistant_id,
                run_config,
                run_context,
            )
            return

        logger.info("[Manager] invoking runs.wait(thread_id=%s, text=%r)", thread_id, msg.text[:100])
        run_start = time.time()
        try:
            result = await client.runs.wait(
                thread_id,
                assistant_id,
                input={"messages": [{"role": "human", "content": msg.text}]},
                config=run_config,
                context=run_context,
                multitask_strategy="reject",
            )
        except Exception as exc:
            if _is_thread_busy_error(exc):
                logger.warning("[Manager] thread busy (concurrent run rejected): thread_id=%s", thread_id)
                await self._send_error(msg, THREAD_BUSY_MESSAGE)
                return
            else:
                raise

        response_text = _extract_response_text(result)
        artifacts = _extract_artifacts(result)

        logger.info(
            "[Manager] agent response received: thread_id=%s, response_len=%d, artifacts=%d",
            thread_id,
            len(response_text) if response_text else 0,
            len(artifacts),
        )

        response_text, attachments = _prepare_artifact_delivery(
            thread_id, response_text, artifacts, msg.channel_name, run_start=run_start
        )

        if not response_text:
            if attachments:
                response_text = _format_artifact_text([a.virtual_path for a in attachments])
            else:
                response_text = "(No response from agent)"

        outbound = OutboundMessage(
            channel_name=msg.channel_name,
            chat_id=msg.chat_id,
            thread_id=thread_id,
            text=response_text,
            artifacts=artifacts,
            attachments=attachments,
            thread_ts=msg.thread_ts,
            metadata=_slim_metadata(msg.metadata),
        )
        logger.info("[Manager] publishing outbound message to bus: channel=%s, chat_id=%s", msg.channel_name, msg.chat_id)
        await self.bus.publish_outbound(outbound)

    async def _handle_streaming_chat(
        self,
        client,
        msg: InboundMessage,
        thread_id: str,
        assistant_id: str,
        run_config: dict[str, Any],
        run_context: dict[str, Any],
    ) -> None:
        logger.info("[Manager] invoking runs.stream(thread_id=%s, text=%r)", thread_id, msg.text[:100])

        run_start = time.time()
        last_values: dict[str, Any] | list | None = None
        streamed_buffers: dict[str, str] = {}
        current_message_id: str | None = None
        latest_text = ""
        last_published_text = ""
        last_publish_at = 0.0
        stream_error: BaseException | None = None
        # [argus patch #10] Telegram gets stage-emoji progress signals instead
        # of streamed partial answer text. Other streaming channels (feishu,
        # wecom) keep getting partial text as before.
        is_telegram = msg.channel_name == "telegram"
        last_stage: str | None = None
        seen_tool = False  # has a tool stage fired this turn (for 'writing' detection)

        try:
            async for chunk in client.runs.stream(
                thread_id,
                assistant_id,
                input={"messages": [{"role": "human", "content": msg.text}]},
                config=run_config,
                context=run_context,
                stream_mode=["messages-tuple", "values"],
                multitask_strategy="reject",
            ):
                event = getattr(chunk, "event", "")
                data = getattr(chunk, "data", None)

                # [argus patch #10] Telegram: emit a coarse stage signal on
                # change (the channel renders it as an animated emoji), and do
                # NOT stream partial answer text. Everything else below is the
                # original streamed-text path for feishu/wecom.
                if is_telegram:
                    stage = _stage_from_chunk(event, data)
                    # Track whether a tool has run this turn, so answer text can
                    # be told apart from initial reasoning.
                    if stage in ("planning", "searching", "working"):
                        seen_tool = True
                    # Promote answer text to a distinct 'writing' stage (early ✍️
                    # signal before the buffered answer lands). Text after a tool
                    # call is the answer being composed; on a no-tool turn the
                    # answer IS the text, so the SECOND text beat (once thinking
                    # has shown) is writing.
                    if stage == "thinking" and (seen_tool or last_stage in ("thinking", "writing")):
                        stage = "writing"
                    if stage and stage != last_stage:
                        last_stage = stage
                        await self.bus.publish_outbound(
                            OutboundMessage(
                                channel_name=msg.channel_name,
                                chat_id=msg.chat_id,
                                thread_id=thread_id,
                                text="",
                                is_final=False,
                                progress_stage=stage,
                                thread_ts=msg.thread_ts,
                                metadata=_slim_metadata(msg.metadata),
                            )
                        )
                    # Still track values for the final result + artifact extraction.
                    if event == "values" and isinstance(data, (dict, list)):
                        last_values = data
                    continue

                if event == "messages-tuple":
                    accumulated_text, current_message_id = _accumulate_stream_text(streamed_buffers, current_message_id, data)
                    if accumulated_text:
                        latest_text = accumulated_text
                elif event == "values" and isinstance(data, (dict, list)):
                    last_values = data
                    snapshot_text = _extract_response_text(data)
                    if snapshot_text:
                        latest_text = snapshot_text

                if not latest_text or latest_text == last_published_text:
                    continue

                now = time.monotonic()
                if last_published_text and now - last_publish_at < STREAM_UPDATE_MIN_INTERVAL_SECONDS:
                    continue

                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel_name=msg.channel_name,
                        chat_id=msg.chat_id,
                        thread_id=thread_id,
                        text=latest_text,
                        is_final=False,
                        thread_ts=msg.thread_ts,
                        metadata=_slim_metadata(msg.metadata),
                    )
                )
                last_published_text = latest_text
                last_publish_at = now
        except Exception as exc:
            stream_error = exc
            if _is_thread_busy_error(exc):
                logger.warning("[Manager] thread busy (concurrent run rejected): thread_id=%s", thread_id)
            else:
                logger.exception("[Manager] streaming error: thread_id=%s", thread_id)
        finally:
            result = last_values if last_values is not None else {"messages": [{"type": "ai", "content": latest_text}]}
            response_text = _extract_response_text(result)
            artifacts = _extract_artifacts(result)
            response_text, attachments = _prepare_artifact_delivery(
                thread_id, response_text, artifacts, msg.channel_name, run_start=run_start
            )

            if not response_text:
                if attachments:
                    response_text = _format_artifact_text([attachment.virtual_path for attachment in attachments])
                elif stream_error:
                    if _is_thread_busy_error(stream_error):
                        response_text = THREAD_BUSY_MESSAGE
                    else:
                        response_text = "An error occurred while processing your request. Please try again."
                else:
                    response_text = latest_text or "(No response from agent)"

            logger.info(
                "[Manager] streaming response completed: thread_id=%s, response_len=%d, artifacts=%d, error=%s",
                thread_id,
                len(response_text),
                len(artifacts),
                stream_error,
            )
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel_name=msg.channel_name,
                    chat_id=msg.chat_id,
                    thread_id=thread_id,
                    text=response_text,
                    artifacts=artifacts,
                    attachments=attachments,
                    is_final=True,
                    thread_ts=msg.thread_ts,
                    metadata=_slim_metadata(msg.metadata),
                )
            )

    # -- command handling --------------------------------------------------

    async def _handle_command(self, msg: InboundMessage) -> None:
        text = msg.text.strip()
        parts = text.split(maxsplit=1)
        command = parts[0].lower().lstrip("/")

        if command == "bootstrap":
            from dataclasses import replace as _dc_replace

            chat_text = parts[1] if len(parts) > 1 else "Initialize workspace"
            chat_msg = _dc_replace(msg, text=chat_text, msg_type=InboundMessageType.CHAT)
            await self._handle_chat(chat_msg, extra_context={"is_bootstrap": True})
            return

        if command == "new":
            # Create a new thread through Gateway
            client = self._get_client()
            thread = await client.threads.create()
            new_thread_id = thread["thread_id"]
            self.store.set_thread_id(
                msg.channel_name,
                msg.chat_id,
                new_thread_id,
                topic_id=msg.topic_id,
                user_id=msg.user_id,
            )
            reply = "New conversation started."
        elif command == "status":
            thread_id = self.store.get_thread_id(msg.channel_name, msg.chat_id, topic_id=msg.topic_id)
            reply = f"Active thread: {thread_id}" if thread_id else "No active conversation."
        elif command == "models":
            reply = await self._fetch_gateway("/api/models", "models")
        elif command == "memory":
            reply = await self._fetch_gateway("/api/memory", "memory")
        elif command == "help":
            reply = (
                "Available commands:\n"
                "/bootstrap — Start a bootstrap session (enables agent setup)\n"
                "/new — Start a new conversation\n"
                "/status — Show current thread info\n"
                "/models — List available models\n"
                "/memory — Show memory status\n"
                "/help — Show this help"
            )
        else:
            available = " | ".join(sorted(KNOWN_CHANNEL_COMMANDS))
            reply = f"Unknown command: /{command}. Available commands: {available}"

        outbound = OutboundMessage(
            channel_name=msg.channel_name,
            chat_id=msg.chat_id,
            thread_id=self.store.get_thread_id(msg.channel_name, msg.chat_id) or "",
            text=reply,
            thread_ts=msg.thread_ts,
            metadata=_slim_metadata(msg.metadata),
        )
        await self.bus.publish_outbound(outbound)

    async def _fetch_gateway(self, path: str, kind: str) -> str:
        """Fetch data from the Gateway API for command responses."""
        import httpx

        try:
            async with httpx.AsyncClient() as http:
                resp = await http.get(
                    f"{self._gateway_url}{path}",
                    timeout=10,
                    headers=create_internal_auth_headers(),
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception:
            logger.exception("Failed to fetch %s from gateway", kind)
            return f"Failed to fetch {kind} information."

        if kind == "models":
            names = [m["name"] for m in data.get("models", [])]
            return ("Available models:\n" + "\n".join(f"• {n}" for n in names)) if names else "No models configured."
        elif kind == "memory":
            facts = data.get("facts", [])
            return f"Memory contains {len(facts)} fact(s)."
        return str(data)

    # -- error helper ------------------------------------------------------

    async def _send_error(self, msg: InboundMessage, error_text: str) -> None:
        outbound = OutboundMessage(
            channel_name=msg.channel_name,
            chat_id=msg.chat_id,
            thread_id=self.store.get_thread_id(msg.channel_name, msg.chat_id) or "",
            text=error_text,
            thread_ts=msg.thread_ts,
            metadata=_slim_metadata(msg.metadata),
        )
        await self.bus.publish_outbound(outbound)

"""ChannelManager — consumes inbound messages and dispatches them to the DeerFlow agent via Gateway."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from langgraph_sdk.errors import ConflictError

from app.channels.commands import KNOWN_CHANNEL_COMMANDS
from app.channels.message_bus import (
    PENDING_CLARIFICATION_METADATA_KEY,
    InboundMessage,
    InboundMessageType,
    MessageBus,
    OutboundMessage,
    ResolvedAttachment,
)
from app.channels.store import ChannelStore
from app.gateway.csrf_middleware import CSRF_COOKIE_NAME, CSRF_HEADER_NAME, generate_csrf_token
from app.gateway.internal_auth import create_internal_auth_headers
from deerflow.config.agents_config import load_agent_config
from deerflow.config.paths import make_safe_user_id
from deerflow.runtime.user_context import get_effective_user_id
from deerflow.skills.slash import parse_slash_skill_reference
from deerflow.skills.storage import get_or_new_skill_storage
from deerflow.skills.storage.skill_storage import SkillStorage
from deerflow.utils.messages import ORIGINAL_USER_CONTENT_KEY

logger = logging.getLogger(__name__)

DEFAULT_LANGGRAPH_URL = "http://localhost:8001/api"
DEFAULT_GATEWAY_URL = "http://localhost:8001"
DEFAULT_ASSISTANT_ID = "lead_agent"
CUSTOM_AGENT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9-]+$")

# Lead-agent recursion budget (LangGraph super-steps for the lead graph only).
# This is independent of subagent depth: a `task()` dispatch runs the whole
# subagent inside ONE lead tools-node step, and subagents enforce their own
# limit via `subagents.max_turns` (see SubagentExecutor). Do not conflate this
# 100 with the general-purpose subagent's max_turns.
DEFAULT_RUN_CONFIG: dict[str, Any] = {"recursion_limit": 100}
DEFAULT_RUN_CONTEXT: dict[str, Any] = {
    "thinking_enabled": True,
    "is_plan_mode": False,
    "subagent_enabled": False,
}
STREAM_UPDATE_MIN_INTERVAL_SECONDS = 0.35
# Stream modes requested from the runtime, and the SSE event names under which
# the message-tuple stream may arrive: the embedded runtime (and LangGraph
# Platform) deliver the requested "messages-tuple" mode as event "messages".
STREAM_MODES = ["messages-tuple", "values"]
MESSAGE_STREAM_EVENTS = ("messages-tuple", "messages")
THREAD_BUSY_MESSAGE = "This conversation is already processing another request. Please wait for it to finish and try again."
BOUND_IDENTITY_REQUIRED_MESSAGE = "Connect this channel from DeerFlow Settings, complete the in-channel connect step, then send your message again."
BOUND_IDENTITY_UNAVAILABLE_MESSAGE = "Channel connection verification is temporarily unavailable. Please try again later or contact the DeerFlow operator."
INBOUND_DEDUPE_TTL_SECONDS = 10 * 60
INBOUND_DEDUPE_MAX_ENTRIES = 4096
# Only server-stable provider message ids: client-generated ids (client_msg_id,
# client_id) are not guaranteed identical across a provider's own redelivery, so
# keying dedupe on them would miss exactly the retries we want to absorb.
INBOUND_DEDUPE_METADATA_KEYS = ("event_id", "message_id", "msg_id")

CHANNEL_CAPABILITIES = {
    "dingtalk": {"supports_streaming": False},
    "discord": {"supports_streaming": False},
    "feishu": {"supports_streaming": True},
    "slack": {"supports_streaming": False},
    "telegram": {"supports_streaming": True},
    "wechat": {"supports_streaming": False},
    "wecom": {"supports_streaming": True},
}

InboundFileReader = Callable[[dict[str, Any], httpx.AsyncClient], Awaitable[bytes | None]]

_METADATA_DROP_KEYS = frozenset({"raw_message", "ref_msg"})


def _slim_metadata(meta: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of *meta* with known-large keys removed."""
    return {k: v for k, v in meta.items() if k not in _METADATA_DROP_KEYS}


# [argus patch #34] A model told to "stay silent" on a contentless unattended
# turn often narrates the decision instead of emitting nothing — e.g. the hourly
# meeting-prep poll posting "No meetings in the window. Staying silent." Such a
# message is itself the noise the silence branch exists to suppress. Match it as
# a SHORT one-liner (real briefs are longer, multi-line, with names/times) whose
# only content is a "nothing to report" and/or "staying silent" announcement.
# Tight on purpose: requires the announcement phrasing AND a length cap, so a
# genuine brief that merely mentions a meeting or the word "silent" is preserved.
_SILENCE_ANNOUNCEMENT_RE = re.compile(
    r"""
    ^\W*(?:
        (?:no|nothing|none)\b .*? \b(?:meeting|meetings|event|events|
            report|update|updates|upcoming|scheduled|to\s+report|to\s+share)\b
      | (?:staying|stay|remaining|going|i'?ll\s+stay|i\s+will\s+stay)\s+silent\b
      | no\s+output\b
    ) .*$
    """,
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)
# A real brief is never this short; the disobedient one-liner is. Cap well above
# the longest plausible announcement, well below any actual brief.
_SILENCE_ANNOUNCEMENT_MAX_LEN = 120


def _is_silence_announcement(stripped: str) -> bool:
    """True if *stripped* is a short 'nothing to report / staying silent'
    announcement rather than a real brief. Conservative: length-capped and
    phrasing-anchored so genuine briefs are never matched."""
    if len(stripped) > _SILENCE_ANNOUNCEMENT_MAX_LEN:
        return False
    return _SILENCE_ANNOUNCEMENT_RE.match(stripped) is not None


def _is_trivial_unattended_text(text: str | None) -> bool:
    """[argus patch #31/#32/#34] True if *text* carries no real content for an
    unattended (scheduled) turn.

    Patch #31 suppressed a fully-empty unattended response. Patch #32 added the
    single filler token a model emits instead of nothing — a ``.``, ``-``,
    ``…``, an ellipsis, or whitespace — which slips past an ``if not
    response_text`` check and gets delivered every cron tick (e.g. the hourly
    meeting-prep poll posting a lone ``.``). Patch #34 adds the next escalation:
    a model that NARRATES its silence in a full sentence ("No meetings in the
    window. Staying silent.") — itself the noise the silence branch exists to
    drop. Treat all three as empty so the unattended-suppression branch fires.

    Conservative: blanks (a) empty/whitespace-only, (b) a short run (<= 3 chars)
    with no alphanumeric character, or (c) a short, phrasing-anchored
    "nothing to report / staying silent" announcement. Any real word, number,
    or longer brief is preserved. Applied ONLY on the unattended path;
    interactive turns are unaffected.
    """
    stripped = (text or "").strip()
    if not stripped:
        return True
    if len(stripped) <= 3 and re.search(r"\w", stripped) is None:
        return True
    return _is_silence_announcement(stripped)


# [argus patch #10/#24] Tool-name hints that map a tool call to the "searching" stage.
_SEARCH_TOOL_HINTS = ("search", "web", "tavily", "exa", "browse", "fetch_url")


def _stage_from_chunk(event: str, data: Any) -> str | None:
    """[argus patch #10/#24] Derive a coarse execution stage from one langgraph
    stream chunk, for the live Telegram progress indicator. Returns one of
    received/thinking/planning/searching/working, or None if the chunk implies
    no stage change.

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


class SlashSkillCommandResolutionError(RuntimeError):
    """Raised when IM slash-skill command resolution cannot complete safely."""


@dataclass(frozen=True, slots=True)
class _SlashSkillCommandResolution:
    route_to_chat: bool = False
    failure_message: str | None = None


@dataclass(frozen=True, slots=True)
class _BoundIdentityRejection:
    message: str = BOUND_IDENTITY_REQUIRED_MESSAGE
    # Server-side connection id that may be used only as an outbound routing
    # hint for the rejection message. This is never copied from the inbound
    # message; it comes from the repository re-read when available.
    outbound_connection_id: str | None = None
    # Server-side owner for the outbound routing connection above. It lets
    # channel senders preserve per-connection context without trusting the
    # rejected inbound identity assertion.
    outbound_owner_user_id: str | None = None


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
            if _is_hidden_human_control_message(msg):
                continue
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


def _messages_from_result(result: dict | list) -> list[Any]:
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        messages = result.get("messages", [])
        if isinstance(messages, list):
            return messages
    return []


def _current_turn_messages(result: dict | list) -> list[dict[str, Any]]:
    messages = _messages_from_result(result)
    current_turn: list[dict[str, Any]] = []
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("type") == "human":
            break
        current_turn.append(msg)
    current_turn.reverse()
    return current_turn


def _has_current_turn_clarification(result: dict | list) -> bool:
    """Return True only when the current turn's final result is clarification."""
    for msg in reversed(_current_turn_messages(result)):
        msg_type = msg.get("type")
        if msg_type == "tool":
            return msg.get("name") == "ask_clarification"
        if msg_type == "ai":
            content = msg.get("content")
            if isinstance(content, str):
                if content:
                    return False
            elif content:
                return False
            if msg.get("tool_calls"):
                return False
    return False


def _response_metadata(base_metadata: dict[str, Any], *, pending_clarification: bool = False) -> dict[str, Any]:
    metadata = _slim_metadata(base_metadata)
    if pending_clarification:
        metadata[PENDING_CLARIFICATION_METADATA_KEY] = True
    return metadata


def _thread_channel_metadata(msg: InboundMessage) -> dict[str, Any]:
    channel_source: dict[str, Any] = {
        "type": "im_channel",
        "provider": msg.channel_name,
        "chat_id": msg.chat_id,
    }
    if msg.topic_id:
        channel_source["topic_id"] = msg.topic_id
    if msg.thread_ts:
        channel_source["thread_ts"] = msg.thread_ts
    if msg.connection_id:
        channel_source["connection_id"] = msg.connection_id

    return {"channel_source": channel_source}


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
            if _is_hidden_human_control_message(msg):
                continue
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


def _is_hidden_human_control_message(msg: Mapping[str, Any]) -> bool:
    """Return whether a human message is an internal control message hidden from UI."""
    if msg.get("type") != "human":
        return False

    additional_kwargs = msg.get("additional_kwargs")
    if not isinstance(additional_kwargs, Mapping):
        return False

    return additional_kwargs.get("hide_from_ui") is True


def _format_artifact_text(artifacts: list[str]) -> str:
    """Format artifact paths into a human-readable text block listing filenames."""
    import posixpath

    filenames = [posixpath.basename(p) for p in artifacts]
    if len(filenames) == 1:
        return f"Created File: 📎 {filenames[0]}"
    return "Created Files: 📎 " + "、".join(filenames)


_OUTPUTS_VIRTUAL_PREFIX = "/mnt/user-data/outputs/"


def _unknown_command_reply(command: str | None = None) -> str:
    available = " | ".join(sorted(KNOWN_CHANNEL_COMMANDS))
    if command:
        return f"Unknown command: /{command}. Available commands: {available}"
    return f"Unknown command. Available commands: {available}"


def _human_input_message(content: str, *, original_content: str | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "human", "content": content}
    if original_content is not None and original_content != content:
        message["additional_kwargs"] = {ORIGINAL_USER_CONTENT_KEY: original_content}
    return message


def _auth_disabled_owner_user_id() -> str | None:
    try:
        from app.gateway.auth_disabled import AUTH_DISABLED_USER_ID, is_auth_disabled
    except Exception:
        logger.debug("Unable to inspect auth-disabled mode for channel owner fallback", exc_info=True)
        return None
    return AUTH_DISABLED_USER_ID if is_auth_disabled() else None


def _effective_owner_user_id(msg: InboundMessage) -> str | None:
    return _auth_disabled_owner_user_id() or msg.owner_user_id


def _apply_effective_owner(msg: InboundMessage) -> InboundMessage:
    owner_user_id = _effective_owner_user_id(msg)
    if owner_user_id:
        msg.owner_user_id = owner_user_id
    return msg


def _owner_headers(msg: InboundMessage) -> dict[str, str] | None:
    owner_user_id = _effective_owner_user_id(msg)
    if not owner_user_id:
        return None
    return create_internal_auth_headers(owner_user_id=owner_user_id)


def _safe_user_id_for_run(raw_user_id: str) -> str:
    from deerflow.config.paths import get_paths

    try:
        return get_paths().prepare_user_dir_for_raw_id(raw_user_id)
    except Exception:
        logger.exception("Failed to prepare channel run user directory")
        return make_safe_user_id(raw_user_id)


def _channel_storage_user_id(msg: InboundMessage) -> str | None:
    """Resolve the canonical DeerFlow user id for a channel-triggered message.

    Single source of truth for both the agent **run identity**
    (``_resolve_run_params`` → ``run_context["user_id"]``) and the **file/artifact
    storage bucket** (``receive_file`` / ``_ingest_inbound_files`` /
    ``_prepare_artifact_delivery``), so the bucket the agent reads/writes always
    matches where channel files are staged. Prefer the bound DeerFlow owner,
    otherwise fall back to the sanitized raw platform user id. Without that
    fallback, an unbound auth-enabled channel would run under ``safe(msg.user_id)``
    but stage files under ``get_effective_user_id()`` (the dispatcher task's unset
    contextvar → ``"default"``), so uploads would land in ``users/default/...``
    while the agent reads ``users/{safe_platform_user_id}/...``. Returns ``None``
    only when neither identity is available, leaving the caller to fall back to the
    contextvar/default user.

    Distinct from :func:`_owner_headers`, which deliberately sends the *raw* owner
    id (no sanitize, no platform fallback) over HTTP for gateway to re-resolve;
    this helper is the in-process, sanitized, filesystem-facing identity.
    """
    owner_user_id = _effective_owner_user_id(msg)
    if owner_user_id:
        return _safe_user_id_for_run(owner_user_id)
    if msg.user_id:
        return _safe_user_id_for_run(msg.user_id)
    return None


def _resolve_slash_skill_command(
    text: str,
    available_skills: set[str] | None = None,
    storage: SkillStorage | Callable[[], SkillStorage] | None = None,
) -> _SlashSkillCommandResolution | None:
    reference = parse_slash_skill_reference(text)
    if reference is None:
        return None
    try:
        resolved_storage = storage() if callable(storage) else storage or get_or_new_skill_storage()
        skills = resolved_storage.load_skills(enabled_only=False)

        skill = next((candidate for candidate in skills if candidate.name == reference.name), None)
        if skill is None:
            return None
        if not skill.enabled:
            return _SlashSkillCommandResolution(failure_message=f"Skill `/{reference.name}` is installed but disabled. Enable it before using slash activation.")
        if available_skills is not None and reference.name not in available_skills:
            return _SlashSkillCommandResolution(failure_message=f"Skill `/{reference.name}` is not available for this agent.")

        return _SlashSkillCommandResolution(route_to_chat=True)
    except Exception as exc:
        logger.exception("[Manager] failed to resolve slash skill command")
        raise SlashSkillCommandResolutionError("Failed to resolve slash skill command. Please check the skill configuration.") from exc


def _resolve_attachments(thread_id: str, artifacts: list[str], *, user_id: str | None = None) -> list[ResolvedAttachment]:
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
    effective_user_id = user_id or get_effective_user_id()
    outputs_dir = paths.sandbox_outputs_dir(thread_id, user_id=effective_user_id).resolve()
    for virtual_path in artifacts:
        # Security: only allow files from the agent outputs directory
        if not virtual_path.startswith(_OUTPUTS_VIRTUAL_PREFIX):
            logger.warning("[Manager] rejected non-outputs artifact path: %s", virtual_path)
            continue
        try:
            actual = paths.resolve_virtual_path(thread_id, virtual_path, user_id=effective_user_id)
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


def _prepare_artifact_delivery(
    thread_id: str,
    response_text: str,
    artifacts: list[str],
    channel_name: str | None = None,
    *,
    user_id: str | None = None,
) -> tuple[str, list[ResolvedAttachment]]:
    """Resolve attachments and append filename fallbacks to the text response.

    [argus patch #10] When the target channel is Telegram, hand off to the
    channel-aware presenter (_artifact_presenter.present_artifacts): it turns
    web-viewable artifacts (HTML/SVG) into VIEWABLE links to the per-stack
    /f/ fileserver instead of attaching the raw file, and links+attaches other
    binaries. For every other channel the original behavior is unchanged.

    (This presenter hand-off was dropped when the owner-scoping refactor #3579
    rewrote this function upstream; restored here so chat channels get a
    clickable remote link again instead of a bare filename.)
    """
    attachments: list[ResolvedAttachment] = []
    if not artifacts:
        return response_text, attachments

    attachments = _resolve_attachments(thread_id, artifacts, user_id=user_id)

    # [argus] Channel-aware presentation for Telegram.
    if channel_name == "telegram":
        from app.channels._artifact_presenter import present_artifacts

        block, attachments = present_artifacts(channel_name, thread_id, artifacts, attachments)
        if block:
            response_text = (response_text + "\n\n" + block) if response_text else block
        return response_text, attachments

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


async def _ingest_inbound_files(thread_id: str, msg: InboundMessage, *, user_id: str | None = None) -> list[dict[str, Any]]:
    if not msg.files:
        return []

    from deerflow.uploads.manager import (
        UnsafeUploadPathError,
        claim_unique_filename,
        ensure_uploads_dir,
        normalize_filename,
        write_upload_file_no_symlink,
    )

    def _prepare_uploads_dir() -> tuple[Path, set[str]]:
        # Worker thread: ensure_uploads_dir's mkdir and the iterdir enumeration are
        # blocking filesystem IO that must stay off the event loop.
        target = ensure_uploads_dir(thread_id, user_id=user_id)
        existing = {entry.name for entry in target.iterdir() if entry.is_file()}
        return target, existing

    uploads_dir, seen_names = await asyncio.to_thread(_prepare_uploads_dir)

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
                dest = await asyncio.to_thread(write_upload_file_no_symlink, uploads_dir, safe_name, data)
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
        connection_repo: Any | None = None,
        require_bound_identity: bool = False,
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
        self._connection_repo = connection_repo
        self._require_bound_identity = require_bound_identity
        self._client = None  # lazy init — langgraph_sdk async client
        self._channel_metadata_synced: set[str] = set()
        self._skill_storage: SkillStorage | None = None
        self._csrf_token = generate_csrf_token()
        self._semaphore: asyncio.Semaphore | None = None
        self._running = False
        self._task: asyncio.Task | None = None
        # Insertion order == chronological (keys are never re-inserted), so an
        # OrderedDict lets us evict expired/overflow entries from the front in
        # O(k) instead of scanning all entries on every inbound message.
        self._recent_inbound_events: OrderedDict[tuple[str, str, str, str], float] = OrderedDict()

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

        # ``user_id`` drives DeerFlow-owned memory, files, and thread buckets.
        # For browser-connected IM channels, prefer the DeerFlow account that
        # owns the connection. Preserve the raw platform user under
        # ``channel_user_id`` for platform-facing lookups and audits.
        run_context_identity: dict[str, Any] = {"thread_id": thread_id}
        # Single source of truth for the run identity: the same helper that scopes
        # inbound files and outbound artifacts, so the bucket the agent reads/writes
        # always matches where channel files are staged.
        run_user_id = _channel_storage_user_id(msg)
        if run_user_id:
            run_context_identity["user_id"] = run_user_id
        if msg.user_id:
            run_context_identity["channel_user_id"] = msg.user_id
        # [argus patch #21/#24] Surface the rest of the channel sender identity so
        # tools can attribute an action to the requesting human (e.g. the Pythia
        # correct_minutes tool authors the minutes commit as the asking staff
        # member). channel_user_id is set above (upstream-native); these three are
        # the remaining additive plumbing, whitelisted into ToolRuntime.context by
        # _CONTEXT_CONFIGURABLE_KEYS in gateway/services.py.
        run_context_identity["channel_name"] = msg.channel_name
        run_context_identity["channel_id"] = msg.chat_id
        if msg.thread_ts:
            run_context_identity["thread_ts"] = msg.thread_ts

        run_context = _merge_dicts(
            DEFAULT_RUN_CONTEXT,
            self._default_session.get("context"),
            channel_layer.get("context"),
            user_layer.get("context"),
            run_context_identity,
        )

        # [argus patch #30] Per-message agent override wins ahead of the channel
        # layers. A scheduled playbook (§3a) pins its own runner (default
        # `atlas`) on the InboundMessage, independent of the channel's pinned
        # agent (telegram -> pythia-internal). When set it takes precedence over
        # the user/channel/default `assistant_id` resolved above.
        if isinstance(msg.agent_name, str) and msg.agent_name.strip():
            assistant_id = _normalize_custom_agent_name(msg.agent_name)

        # Custom agents are implemented as lead_agent + agent_name context.
        # Keep backward compatibility for channel configs that set
        # assistant_id: <custom-agent-name> by routing through lead_agent.
        if assistant_id != DEFAULT_ASSISTANT_ID:
            # [argus patch #30] A per-message override must win even when the
            # channel/default layer already seeded an agent_name, so set (not
            # setdefault) when it came from msg.agent_name.
            if isinstance(msg.agent_name, str) and msg.agent_name.strip():
                run_context["agent_name"] = assistant_id
            else:
                run_context.setdefault("agent_name", _normalize_custom_agent_name(assistant_id))
            assistant_id = DEFAULT_ASSISTANT_ID

        # [argus patch #30] Per-job memory policy (§4b). Unattended turns (a
        # scheduled fire) default to memory read-only so a job never silently
        # mutates the citizen's long-term memory; an explicit `memory_mode`
        # overrides. Both ride run_context -> runtime.context, read by
        # MemoryMiddleware.after_agent and DynamicContextMiddleware.
        if msg.unattended:
            run_context["unattended"] = True
        memory_mode = msg.memory_mode or ("read-only" if msg.unattended else None)
        if memory_mode:
            run_context["memory_mode"] = memory_mode

        return assistant_id, run_config, run_context

    def _resolve_available_skill_names(self, msg: InboundMessage) -> set[str] | None:
        thread_id = self.store.get_thread_id(msg.channel_name, msg.chat_id, topic_id=msg.topic_id) or ""
        _, _, run_context = self._resolve_run_params(msg, thread_id)
        if run_context.get("is_bootstrap"):
            return {"bootstrap"}

        agent_name = run_context.get("agent_name")
        if not isinstance(agent_name, str) or not agent_name.strip():
            return None

        agent_config = load_agent_config(_normalize_custom_agent_name(agent_name))
        if agent_config and agent_config.skills is not None:
            return set(agent_config.skills)
        return None

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

    def _get_skill_storage(self) -> SkillStorage:
        if self._skill_storage is None:
            self._skill_storage = get_or_new_skill_storage()
        return self._skill_storage

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

            # Dedupe before logging "received" so a provider retrying an event N
            # times does not log N accepts; duplicates are logged once as ignored.
            # Note: this manager-level dedupe only guards the agent run / final
            # answer. Provider adapters may emit ack side-effects (a "Working on
            # it…" reply, an "eyes" reaction) before publish_inbound, so those are
            # intentionally not deduped here.
            if self._is_duplicate_inbound(msg):
                continue
            logger.info(
                "[Manager] received inbound: channel=%s, chat_id=%s, type=%s, text_len=%d, files=%d",
                msg.channel_name,
                msg.chat_id,
                msg.msg_type.value,
                len(msg.text or ""),
                len(msg.files),
            )
            # [argus patch #10] Coalesce CHAT messages per conversation so a
            # split paste (Telegram chunks a long message into several) becomes
            # ONE agent turn instead of racing on the same thread (the 2nd+ would
            # 409 "thread busy" and be lost). Commands bypass coalescing — a /new
            # must run immediately and never merge.
            if self._coalescer is not None and msg.msg_type == InboundMessageType.CHAT:
                self._coalescer.add(msg)
            else:
                # Commands, or coalescing disabled (window<=0) -> dispatch now.
                await self._dispatch_handle(msg)

    async def _dispatch_handle(self, msg: InboundMessage) -> None:
        """[argus patch #10] Spawn the actual message handler (used directly for
        commands and as the coalescer's dispatch callback for combined chat
        turns). Async so the coalescer can `await` it; returns as soon as the
        task is scheduled."""
        task = asyncio.create_task(self._handle_message(msg))
        task.add_done_callback(self._log_task_error)

    @staticmethod
    def _inbound_dedupe_key(msg: InboundMessage) -> tuple[str, str, str, str] | None:
        metadata = msg.metadata or {}
        message_id = None
        for key in INBOUND_DEDUPE_METADATA_KEYS:
            value = metadata.get(key)
            if value:
                message_id = str(value)
                break
        if message_id is None:
            raw_message = metadata.get("raw_message")
            if isinstance(raw_message, Mapping):
                for key in INBOUND_DEDUPE_METADATA_KEYS:
                    value = raw_message.get(key)
                    if value:
                        message_id = str(value)
                        break
        if message_id is None:
            return None

        # Fail closed: without a workspace/team/guild identifier we cannot tell two
        # workspaces apart (e.g. Slack channel ids are not globally unique), so
        # skip dedupe rather than risk collapsing distinct workspaces' messages.
        workspace_id = msg.workspace_id or metadata.get("workspace_id") or metadata.get("team_id") or metadata.get("guild_id") or metadata.get("aibotid")
        if not workspace_id:
            return None
        return (msg.channel_name, str(workspace_id), msg.chat_id, message_id)

    def _is_duplicate_inbound(self, msg: InboundMessage) -> bool:
        key = self._inbound_dedupe_key(msg)
        if key is None:
            return False

        now = time.monotonic()
        # Entries are in chronological insertion order, so expired ones cluster at
        # the front: pop from the front until we hit a still-live entry.
        while self._recent_inbound_events:
            _, oldest_at = next(iter(self._recent_inbound_events.items()))
            if now - oldest_at > INBOUND_DEDUPE_TTL_SECONDS:
                self._recent_inbound_events.popitem(last=False)
            else:
                break
        while len(self._recent_inbound_events) > INBOUND_DEDUPE_MAX_ENTRIES:
            self._recent_inbound_events.popitem(last=False)

        if key in self._recent_inbound_events:
            logger.info(
                "[Manager] duplicate inbound ignored: channel=%s, chat_id=%s, message_id=%s",
                msg.channel_name,
                msg.chat_id,
                key[-1],
            )
            return True

        self._recent_inbound_events[key] = now
        return False

    def _release_inbound_dedupe_key(self, msg: InboundMessage) -> None:
        """Drop a recorded dedupe key so a provider redelivery can be reprocessed.

        Called only on transient/unexpected handling failures: the key was
        recorded on receipt so retries arriving *while* the message is being
        handled are still deduped, but if handling fails we must not turn a
        recoverable error into a TTL-long black hole for the same message_id.
        """
        key = self._inbound_dedupe_key(msg)
        if key is not None:
            self._recent_inbound_events.pop(key, None)

    @staticmethod
    def _log_task_error(task: asyncio.Task) -> None:
        """Surface unhandled exceptions from background tasks."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error("[Manager] unhandled error in message task: %s", exc, exc_info=exc)

    async def _handle_message(self, msg: InboundMessage) -> None:
        msg = _apply_effective_owner(msg)
        try:
            # Non-command chat can be rejected before it consumes a semaphore
            # slot. Commands are handled below because provider adapters consume
            # binding commands before manager dispatch, and _handle_command()
            # applies its own admission gate for manager-level commands.
            bound_identity_rejection = None
            if msg.msg_type != InboundMessageType.COMMAND:
                bound_identity_rejection = await self._get_bound_identity_rejection(msg)
            if bound_identity_rejection is not None:
                await self._reject_unbound_channel_message(msg, bound_identity_rejection=bound_identity_rejection)
                return

            async with self._semaphore:
                if msg.msg_type == InboundMessageType.COMMAND:
                    await self._handle_command(msg)
                else:
                    await self._handle_chat(msg, bound_identity_checked=True)
        except InvalidChannelSessionConfigError as exc:
            logger.warning(
                "Invalid channel session config for %s (chat=%s): %s",
                msg.channel_name,
                msg.chat_id,
                exc,
            )
            await self._send_error(msg, str(exc))
        except SlashSkillCommandResolutionError as exc:
            logger.warning(
                "Slash skill command resolution failed for %s (chat=%s): %s",
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
            # Transient/unexpected failure: release the dedupe key so a provider
            # redelivery of the same message can recover instead of being dropped
            # for the dedupe TTL.
            self._release_inbound_dedupe_key(msg)
            await self._send_error(msg, "An internal error occurred. Please try again.")

    # -- chat handling -----------------------------------------------------

    async def _get_bound_identity_rejection(self, msg: InboundMessage) -> _BoundIdentityRejection | None:
        """Return None when *msg* may proceed; otherwise return rejection routing hints.

        The returned object means the message lacks a verified bound identity.
        Its fields are intentionally limited to server-side values re-read from
        the connection repository, so rejection outbounds never trust a rejected
        inbound message's asserted connection metadata.
        """
        if not self._require_bound_identity:
            return None
        if _auth_disabled_owner_user_id():
            return None

        has_connection = bool(msg.connection_id)
        has_owner = bool(msg.owner_user_id)
        if not (has_connection and has_owner):
            return _BoundIdentityRejection()
        if self._connection_repo is None:
            return _BoundIdentityRejection(message=BOUND_IDENTITY_UNAVAILABLE_MESSAGE)

        # The manager is the run-creation security boundary, so it does not
        # trust mutable InboundMessage identity fields by themselves. Re-read
        # the binding by provider identity before creating DeerFlow threads or
        # runs. If the asserted identity does not match, keep only the
        # server-side connection fields as outbound routing hints.
        connection = await self._connection_repo.find_connection_by_external_identity(
            provider=msg.channel_name,
            external_account_id=msg.user_id,
            workspace_id=msg.workspace_id or None,
        )
        if connection is None:
            return _BoundIdentityRejection()

        connection_id = connection.get("id")
        owner_user_id = connection.get("owner_user_id")
        if connection_id == msg.connection_id and owner_user_id == msg.owner_user_id:
            return None
        return _BoundIdentityRejection(outbound_connection_id=connection_id, outbound_owner_user_id=owner_user_id)

    async def _reject_unbound_channel_message(
        self,
        msg: InboundMessage,
        *,
        bound_identity_rejection: _BoundIdentityRejection,
    ) -> None:
        logger.info(
            "[Manager] rejecting unbound channel message: channel=%s, chat_id=%s",
            msg.channel_name,
            msg.chat_id,
        )
        outbound = OutboundMessage(
            channel_name=msg.channel_name,
            chat_id=msg.chat_id,
            thread_id="",
            text=bound_identity_rejection.message,
            thread_ts=msg.thread_ts,
            connection_id=bound_identity_rejection.outbound_connection_id,
            owner_user_id=bound_identity_rejection.outbound_owner_user_id,
            metadata=_slim_metadata(msg.metadata),
        )
        await self.bus.publish_outbound(outbound)

    async def _lookup_thread_id(self, msg: InboundMessage) -> str | None:
        if msg.connection_id and self._connection_repo is not None:
            return await self._connection_repo.get_thread_id(
                msg.connection_id,
                msg.chat_id,
                msg.topic_id,
            )
        return self.store.get_thread_id(msg.channel_name, msg.chat_id, topic_id=msg.topic_id)

    async def _store_thread_id(self, msg: InboundMessage, thread_id: str) -> None:
        if msg.connection_id and msg.owner_user_id and self._connection_repo is not None:
            await self._connection_repo.set_thread_id(
                connection_id=msg.connection_id,
                owner_user_id=msg.owner_user_id,
                provider=msg.channel_name,
                external_conversation_id=msg.chat_id,
                external_topic_id=msg.topic_id,
                thread_id=thread_id,
            )
            return

        self.store.set_thread_id(
            msg.channel_name,
            msg.chat_id,
            thread_id,
            topic_id=msg.topic_id,
            user_id=msg.user_id,
        )

    async def _create_thread(self, client, msg: InboundMessage) -> str:
        """Create a new thread through Gateway and store the mapping."""
        metadata = _thread_channel_metadata(msg)
        owner_headers = _owner_headers(msg)
        if owner_headers:
            thread = await client.threads.create(metadata=metadata, headers=owner_headers)
        else:
            thread = await client.threads.create(metadata=metadata)
        thread_id = thread["thread_id"]
        await self._store_thread_id(msg, thread_id)
        logger.info("[Manager] new thread created through Gateway: thread_id=%s for chat_id=%s topic_id=%s", thread_id, msg.chat_id, msg.topic_id)
        return thread_id

    async def _update_thread_channel_metadata(self, client, msg: InboundMessage, thread_id: str) -> None:
        """Best-effort source metadata backfill for existing IM-created threads."""
        # The metadata (provider/chat/topic) is constant for a thread, so one
        # successful backfill per manager lifetime is enough — skip the
        # redundant PATCH on every subsequent inbound message.
        if thread_id in self._channel_metadata_synced:
            return
        update_kwargs: dict[str, Any] = {"metadata": _thread_channel_metadata(msg)}
        if owner_headers := _owner_headers(msg):
            update_kwargs["headers"] = owner_headers
        try:
            await client.threads.update(thread_id, **update_kwargs)
        except Exception:
            logger.debug("[Manager] failed to update channel metadata for thread_id=%s", thread_id, exc_info=True)
            return
        if len(self._channel_metadata_synced) > 4096:
            self._channel_metadata_synced.clear()
        self._channel_metadata_synced.add(thread_id)

    async def _handle_chat(
        self,
        msg: InboundMessage,
        extra_context: dict[str, Any] | None = None,
        *,
        bound_identity_checked: bool = False,
    ) -> None:
        # Normal entry paths already run the bound-identity check in
        # _handle_message() or _handle_command(). Keep this default False so
        # direct callers and future internal paths still fail closed.
        bound_identity_rejection = None if bound_identity_checked else await self._get_bound_identity_rejection(msg)
        if bound_identity_rejection is not None:
            await self._reject_unbound_channel_message(msg, bound_identity_rejection=bound_identity_rejection)
            return

        client = self._get_client()
        storage_user_id = _channel_storage_user_id(msg)

        # Look up existing DeerFlow thread.
        # topic_id may be None (e.g. Telegram private chats) — the store
        # handles this by using the "channel:chat_id" key without a topic suffix.
        thread_id = await self._lookup_thread_id(msg)
        if thread_id:
            logger.info("[Manager] reusing thread: thread_id=%s for topic_id=%s", thread_id, msg.topic_id)
            await self._update_thread_channel_metadata(client, msg, thread_id)

        # No existing thread found — create a new one
        if thread_id is None:
            thread_id = await self._create_thread(client, msg)
            # [argus patch #22] This topic has no DeerFlow conversation yet. If it
            # is a reply under an existing thread (e.g. the raw-posted minutes
            # draft, which never created an agent thread), pull the thread's
            # earlier messages so the reply has the context it refers to.
            # Best-effort + only fetches when the channel supports it.
            try:
                from .service import get_channel_service

                channel = (get_channel_service() or None) and get_channel_service().get_channel(msg.channel_name)
                fetch = getattr(channel, "fetch_thread_context", None) if channel else None
                if fetch and msg.topic_id:
                    ctx = await fetch(msg.chat_id, msg.topic_id, (msg.metadata or {}).get("event_ts", ""))
                    if ctx:
                        msg.text = f"{ctx}\n\n{msg.text}".strip()
                        logger.info("[Manager] prepended thread context (%d chars) for new topic_id=%s", len(ctx), msg.topic_id)
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
            msg = await channel.receive_file(msg, thread_id, user_id=storage_user_id) if channel else msg
        if extra_context:
            run_context.update(extra_context)

        original_text = msg.text
        uploaded = await _ingest_inbound_files(thread_id, msg, user_id=storage_user_id)
        if uploaded:
            msg.text = f"{_format_uploaded_files_block(uploaded)}\n\n{msg.text}".strip()
        human_message = _human_input_message(msg.text, original_content=original_text)

        if self._channel_supports_streaming(msg.channel_name):
            await self._handle_streaming_chat(
                client,
                msg,
                thread_id,
                assistant_id,
                run_config,
                run_context,
                human_message,
                storage_user_id=storage_user_id,
            )
            return

        logger.info("[Manager] invoking runs.wait(thread_id=%s, text_len=%d)", thread_id, len(msg.text or ""))
        run_kwargs: dict[str, Any] = {
            "input": {"messages": [human_message]},
            "config": run_config,
            "context": run_context,
            "multitask_strategy": "reject",
        }
        if owner_headers := _owner_headers(msg):
            run_kwargs["headers"] = owner_headers
        try:
            result = await client.runs.wait(
                thread_id,
                assistant_id,
                **run_kwargs,
            )
        except Exception as exc:
            if _is_thread_busy_error(exc):
                logger.warning("[Manager] thread busy (concurrent run rejected): thread_id=%s", thread_id)
                await self._send_error(msg, THREAD_BUSY_MESSAGE)
                return
            else:
                raise

        response_text = _extract_response_text(result)
        pending_clarification = _has_current_turn_clarification(result)
        artifacts = _extract_artifacts(result)

        logger.info(
            "[Manager] agent response received: thread_id=%s, response_len=%d, artifacts=%d",
            thread_id,
            len(response_text) if response_text else 0,
            len(artifacts),
        )

        # Reuse the storage owner cached at the top of _handle_chat so uploads and
        # artifact delivery always resolve to the same bucket, even if a future
        # channel.receive_file returns a rewritten InboundMessage.
        response_text, attachments = _prepare_artifact_delivery(thread_id, response_text, artifacts, msg.channel_name, user_id=storage_user_id)

        # [argus patch #31/#32] Stay silent on a contentless unattended (scheduled)
        # turn. A job with nothing to report (e.g. the hourly meeting-prep poll
        # with no meeting imminent) must not post "(No response from agent)" or a
        # lone filler token to the citizen's chat every tick. Only fires on the
        # unattended path with no real text AND no artifacts; interactive turns
        # and real errors are untouched.
        if msg.unattended and not attachments and _is_trivial_unattended_text(response_text):
            logger.info("[Manager] unattended turn produced no content; staying silent (channel=%s chat_id=%s)", msg.channel_name, msg.chat_id)
            return

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
            connection_id=msg.connection_id,
            owner_user_id=msg.owner_user_id,
            metadata=_response_metadata(msg.metadata, pending_clarification=pending_clarification),
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
        human_message: dict[str, Any],
        storage_user_id: str | None = None,
    ) -> None:
        logger.info("[Manager] invoking runs.stream(thread_id=%s, text_len=%d)", thread_id, len(msg.text or ""))

        last_values: dict[str, Any] | list | None = None
        streamed_buffers: dict[str, str] = {}
        current_message_id: str | None = None
        latest_text = ""
        last_published_text = ""
        last_publish_at = 0.0
        stream_error: BaseException | None = None
        # [argus patch #10/#24] Telegram gets stage-emoji progress signals instead
        # of streamed partial answer text. Other streaming channels (feishu,
        # wecom) keep getting partial text as before.
        is_telegram = msg.channel_name == "telegram"
        last_stage: str | None = None
        seen_tool = False  # has a tool stage fired this turn (for 'writing' detection)
        stream_kwargs: dict[str, Any] = {
            "input": {"messages": [human_message]},
            "config": run_config,
            "context": run_context,
            "stream_mode": list(STREAM_MODES),
            "multitask_strategy": "reject",
        }
        if owner_headers := _owner_headers(msg):
            stream_kwargs["headers"] = owner_headers

        try:
            async for chunk in client.runs.stream(
                thread_id,
                assistant_id,
                **stream_kwargs,
            ):
                event = getattr(chunk, "event", "")
                data = getattr(chunk, "data", None)

                # [argus patch #10/#24] Telegram: emit a coarse stage signal on
                # change (the channel renders it as an animated emoji) and do NOT
                # stream partial answer text. Everything below is the original
                # streamed-text path for feishu/wecom.
                if is_telegram:
                    stage = _stage_from_chunk(event, data)
                    if stage in ("planning", "searching", "working"):
                        seen_tool = True
                    # Promote answer text to a distinct 'writing' stage (early ✍️
                    # before the buffered answer lands): text after a tool call is
                    # the answer being composed; on a no-tool turn the answer IS
                    # the text, so the SECOND text beat (once thinking has shown)
                    # is writing.
                    if stage == "thinking" and (seen_tool or last_stage in ("thinking", "writing")):
                        stage = "writing"
                    # Suppress live stage emojis on unattended (scheduled) turns —
                    # a cron poll shouldn't ping the citizen with indicators,
                    # especially when it ends up silent (patch #31/#32).
                    if stage and stage != last_stage and not getattr(msg, "unattended", False):
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
                                connection_id=msg.connection_id,
                                owner_user_id=msg.owner_user_id,
                                metadata=_response_metadata(msg.metadata),
                            )
                        )
                    # Still track values for the final result + artifact extraction.
                    if event == "values" and isinstance(data, (dict, list)):
                        last_values = data
                    continue

                if event in MESSAGE_STREAM_EVENTS:
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
                        connection_id=msg.connection_id,
                        owner_user_id=msg.owner_user_id,
                        metadata=_response_metadata(msg.metadata),
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
            pending_clarification = _has_current_turn_clarification(result)
            artifacts = _extract_artifacts(result)
            # Reuse the storage owner resolved by _handle_chat so artifact delivery
            # matches the upload bucket and we avoid re-running _safe_user_id_for_run
            # (and its possible filesystem touch) on the streaming-error path.
            response_text, attachments = _prepare_artifact_delivery(thread_id, response_text, artifacts, msg.channel_name, user_id=storage_user_id)

            # [argus patch #31/#32] Stay silent on a contentless unattended turn
            # (see the non-streaming guard). Only when there is no real text, no
            # attachments, AND no stream error — a real error must still be
            # delivered, and interactive turns are untouched. Use a flag (NOT a
            # bare return) because we are inside a finally block: returning here
            # would swallow any in-flight exception from the try body.
            suppress_final = bool(msg.unattended and not attachments and not stream_error and _is_trivial_unattended_text(response_text) and _is_trivial_unattended_text(latest_text))

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

            if suppress_final:
                logger.info("[Manager] unattended streaming turn produced no content; staying silent (channel=%s chat_id=%s)", msg.channel_name, msg.chat_id)
            else:
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
                        connection_id=msg.connection_id,
                        owner_user_id=msg.owner_user_id,
                        metadata=_response_metadata(msg.metadata, pending_clarification=pending_clarification),
                    )
                )

    # -- command handling --------------------------------------------------

    async def _handle_command(self, msg: InboundMessage) -> None:
        # Commands are the other run-creation entry point besides chat: /new
        # calls _create_thread() directly, and /bootstrap routes into
        # _handle_chat(). Apply the same bound-identity admission boundary here
        # so unbound platform users cannot create unowned threads/checkpoints or
        # query Gateway state via commands. Provider-level binding flows
        # (/connect <code>, /start <code>) are consumed by the provider adapter
        # before the message reaches the manager, so they are unaffected.
        bound_identity_rejection = await self._get_bound_identity_rejection(msg)
        if bound_identity_rejection is not None:
            await self._reject_unbound_channel_message(msg, bound_identity_rejection=bound_identity_rejection)
            return

        raw_text = msg.text
        text = raw_text.strip()
        parts = text.split(maxsplit=1)
        reply: str | None = None
        if not parts:
            command = None
            reply = _unknown_command_reply()
        else:
            command = parts[0].lower().removeprefix("/")

        if reply is None and not raw_text.startswith("/"):
            reply = _unknown_command_reply(command)

        if reply is None and command == "bootstrap":
            from dataclasses import replace as _dc_replace

            chat_text = parts[1] if len(parts) > 1 else "Initialize workspace"
            chat_msg = _dc_replace(msg, text=chat_text, msg_type=InboundMessageType.CHAT)
            await self._handle_chat(chat_msg, extra_context={"is_bootstrap": True}, bound_identity_checked=True)
            return

        if reply is None and command == "new":
            # Create a new thread through Gateway
            client = self._get_client()
            await self._create_thread(client, msg)
            reply = "New conversation started."
        elif reply is None and command == "status":
            thread_id = await self._lookup_thread_id(msg)
            reply = f"Active thread: {thread_id}" if thread_id else "No active conversation."
        elif reply is None and command == "models":
            reply = await self._fetch_gateway("/api/models", "models", msg=msg)
        elif reply is None and command == "memory":
            reply = await self._fetch_gateway("/api/memory", "memory", msg=msg)
        elif reply is None and command == "help":
            reply = (
                "Available commands:\n"
                "/bootstrap — Start a bootstrap session (enables agent setup)\n"
                "/new — Start a new conversation\n"
                "/status — Show current thread info\n"
                "/models — List available models\n"
                "/memory — Show memory status\n"
                "/<skill-name> <task> — Activate an enabled skill for one turn\n"
                "/help — Show this help"
            )
        elif reply is None:
            slash_resolution = await asyncio.to_thread(
                lambda: _resolve_slash_skill_command(
                    raw_text,
                    self._resolve_available_skill_names(msg),
                    self._get_skill_storage,
                )
            )
            if slash_resolution and slash_resolution.failure_message:
                reply = slash_resolution.failure_message
            elif slash_resolution and slash_resolution.route_to_chat:
                from dataclasses import replace as _dc_replace

                chat_msg = _dc_replace(msg, msg_type=InboundMessageType.CHAT)
                await self._handle_chat(chat_msg, bound_identity_checked=True)
                return
            else:
                reply = _unknown_command_reply(command)

        outbound = OutboundMessage(
            channel_name=msg.channel_name,
            chat_id=msg.chat_id,
            thread_id=await self._lookup_thread_id(msg) or "",
            text=reply,
            thread_ts=msg.thread_ts,
            connection_id=msg.connection_id,
            owner_user_id=msg.owner_user_id,
            metadata=_slim_metadata(msg.metadata),
        )
        await self.bus.publish_outbound(outbound)

    async def _fetch_gateway(self, path: str, kind: str, *, msg: InboundMessage | None = None) -> str:
        """Fetch data from the Gateway API for command responses."""
        import httpx

        try:
            headers = _owner_headers(msg) if msg is not None else None
            async with httpx.AsyncClient() as http:
                resp = await http.get(
                    f"{self._gateway_url}{path}",
                    timeout=10,
                    headers=headers or create_internal_auth_headers(),
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
            thread_id=await self._lookup_thread_id(msg) or "",
            text=error_text,
            thread_ts=msg.thread_ts,
            connection_id=msg.connection_id,
            owner_user_id=msg.owner_user_id,
            metadata=_slim_metadata(msg.metadata),
        )
        await self.bus.publish_outbound(outbound)

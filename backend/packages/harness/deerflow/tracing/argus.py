"""[argus patch #121] Ariadne: make DeerFlow's Langfuse tracing safe to run for citizens.

Argus runs a shared, self-hosted Langfuse ("Ariadne") where each citizen sees
their own chats, kept 7 days. This module holds what the upstream integration
lacks for that:

* Who: a trace's user is the person's email (the stack owner on a
  single-citizen Atlas stack), not the per-stack DeerFlow user UUID that means
  nothing outside the stack. Tags name the stack and the channel the run came
  from (web, telegram, schedule, ...).
* A per-chat off switch: a thread whose metadata says
  ``ariadne_tracing: "off"`` exports NOTHING, including the memory updater,
  title and subagent calls that run on its behalf. The switch is enforced at
  export (``should_export_span``): every span carries ``session.id`` (= thread
  id) since langfuse 4.7, so one check covers every path that touches the chat.
  The set of switched-off threads is loaded at gateway start and kept current
  by each run, the thread menu action and Telegram ``/notrace``.
* Masking before anything leaves the process: base64 images become a size
  note (view_image re-sends every image on every turn), credentials are
  redacted, and oversized fields are cut, keeping the system prompt and the
  latest messages.

Everything here is fail-open: a tracing problem may cost a trace, never a run.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections.abc import Iterable, Mapping
from typing import Any

logger = logging.getLogger(__name__)

TRACING_METADATA_KEY = "ariadne_tracing"
TRACING_OFF = "off"

# Per string, then per field (a generation's prompt, or a node's state).
STRING_CAP = 128 * 1024
PROMPT_CAP = 1024 * 1024
STATE_CAP = 256 * 1024

_lock = threading.Lock()
_untraced: set[str] = set()
# Threads whose switch a run has read in this process, and whether that is
# required: when the start-up preload fails, the filter fails CLOSED and only
# exports chats whose switch has been read since (privacy over completeness).
_checked: set[str] = set()
_require_check = False


# --- the per-chat switch -----------------------------------------------------
def is_tracing_off(metadata: Mapping[str, Any] | None) -> bool:
    return bool(metadata) and str(metadata.get(TRACING_METADATA_KEY, "")).strip().lower() == TRACING_OFF


def set_thread_untraced(thread_id: str | None, off: bool) -> None:
    if not thread_id:
        return
    with _lock:
        _checked.add(str(thread_id))
        if off:
            _untraced.add(str(thread_id))
        else:
            _untraced.discard(str(thread_id))


def is_thread_untraced(thread_id: str | None) -> bool:
    if not thread_id:
        return False
    with _lock:
        tid = str(thread_id)
        return tid in _untraced or (_require_check and tid not in _checked)


def reset_untraced_threads(thread_ids: Iterable[str] = ()) -> None:
    global _require_check
    with _lock:
        _untraced.clear()
        _untraced.update(str(t) for t in thread_ids if t)
        _checked.clear()
        _require_check = False


def mark_preload_failed() -> None:
    """The switched-off list could not be loaded: export only chats whose
    switch a run has read since."""
    global _require_check
    with _lock:
        _require_check = True


async def preload_untraced_threads(session_factory) -> int:
    """Load every switched-off thread at gateway start (before any run), so a
    memory update right after a restart is not exported either."""
    from sqlalchemy import select

    from deerflow.persistence.json_compat import json_match
    from deerflow.persistence.thread_meta.model import ThreadMetaRow

    async with session_factory() as session:
        rows = await session.execute(select(ThreadMetaRow.thread_id).where(json_match(ThreadMetaRow.metadata_json, TRACING_METADATA_KEY, TRACING_OFF)))
        ids = [r[0] for r in rows]
    reset_untraced_threads(ids)
    return len(ids)


# Where a span carries its chat: propagated trace attributes (under a graph or
# chain root), or, when a bare model call is the root (the memory updater and
# other model-level paths), only the raw callback metadata, which Langfuse
# maps to the session on the server.
_SESSION_KEYS = ("session.id", "langfuse.session.id", "langfuse.observation.metadata.langfuse_session_id", "langfuse.trace.metadata.langfuse_session_id")


def should_export_span(span) -> bool:
    """Langfuse export filter: the SDK default, minus switched-off chats."""
    from langfuse import is_default_export_span

    attrs = getattr(span, "attributes", None) or {}
    for key in _SESSION_KEYS:
        session = attrs.get(key)
        if session and is_thread_untraced(str(session)):
            return False
    return is_default_export_span(span)


# --- who and where -------------------------------------------------------------
def stack_owner_email() -> str | None:
    for name in ("ATLAS_STACK_OWNER_EMAIL", "PYTHIA_CALLER_EMAIL"):
        value = (os.environ.get(name) or "").strip().lower()
        if value:
            return value
    return None


def display_user(user_id: str | None) -> str | None:
    """The email for a trace's user: the signed-in user's own when this run is
    theirs, else the stack owner's (channel, schedule and subagent runs on a
    single-citizen stack run as the owner), else the raw id."""
    try:
        from deerflow.runtime.user_context import get_current_user

        user = get_current_user()
        email = getattr(user, "email", None)
        if email and (user_id is None or str(getattr(user, "id", "")) == str(user_id)):
            return str(email).lower()
    except Exception:  # noqa: BLE001 - attribution is best effort
        pass
    return stack_owner_email() or user_id


def stack_tag() -> str | None:
    project = (os.environ.get("ARGUS_PROJECT") or "").strip()
    return f"stack:{project.removeprefix('atlas-')}" if project else None


def channel_tag(metadata: Mapping[str, Any] | None) -> str:
    origin = (metadata or {}).get("deerflow_origin") or {}
    if not isinstance(origin, Mapping) or not origin.get("kind"):
        return "channel:web"
    kind = str(origin.get("kind"))
    if kind == "im_channel" and origin.get("provider"):
        return f"channel:{origin.get('provider')}"
    return f"channel:{kind}"


# --- masking ---------------------------------------------------------------------
# Same intent and sources as Argus's cerberus/src/redact.py and
# scripts/ariadne-runner-ingest.py (seat tokens, sandbox keys, job passes).
_SECRETS = [
    re.compile(r"\bsk-(?:ant-[a-z0-9]+-|proj-|or-v1-|lf-|sandbox-|proteus-)?[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bajp1\.[A-Za-z0-9._\-]{16,}"),
    re.compile(r"\bak_[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{12,}"),
]
_DATA_URI = re.compile(r"data:([a-z]+/[a-z0-9.+-]+);base64,[A-Za-z0-9+/=]{200,}")


def _mask_text(s: str) -> str:
    s = _DATA_URI.sub(lambda m: f"[{m.group(1)} omitted, {len(m.group(0)) * 3 // 4} bytes]", s)
    for rx in _SECRETS:
        s = rx.sub("[redacted]", s)
    if len(s) > STRING_CAP:
        s = s[:STRING_CAP] + f"\n[... {len(s) - STRING_CAP} more characters cut by Ariadne]"
    return s


def _mask_value(value: Any) -> Any:
    if isinstance(value, str):
        return _mask_text(value)
    if isinstance(value, Mapping):
        if value.get("type") in ("image", "input_image", "image_url") and ("source" in value or "image_url" in value or "data" in value):
            return {"type": "image", "note": "image omitted by Ariadne"}
        return {k: _mask_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mask_value(v) for v in value]
    return value


def _size(value: Any) -> int:
    try:
        return len(json.dumps(value, default=str))
    except Exception:  # noqa: BLE001
        return len(str(value))


def _fit_messages(messages: list, cap: int) -> list:
    """Keep the first message (usually the system prompt) and as many of the
    latest as fit, with a note where the middle was dropped."""
    if _size(messages) <= cap or len(messages) < 3:
        return messages
    head, tail = [messages[0]], []
    budget = cap - _size(head) - 200
    for m in reversed(messages[1:]):
        n = _size(m)
        if n > budget:
            break
        tail.insert(0, m)
        budget -= n
    dropped = len(messages) - 1 - len(tail)
    return head + [{"role": "system", "content": f"[{dropped} earlier messages omitted by Ariadne]"}] + tail


def mask(*, data: Any, **kwargs: Any) -> Any:
    """Langfuse mask function (input, output and metadata of every observation)."""
    try:
        data = _mask_value(data)
        if isinstance(data, list) and data and all(isinstance(m, Mapping) for m in data):
            data = _fit_messages(data, PROMPT_CAP)  # a model call's prompt
        elif isinstance(data, Mapping) and isinstance(data.get("messages"), list):
            data = {**data, "messages": _fit_messages(data["messages"], STATE_CAP)}  # graph state
        if _size(data) > PROMPT_CAP:
            text = json.dumps(data, default=str)
            data = text[:PROMPT_CAP] + f"[... {len(text) - PROMPT_CAP} more characters cut by Ariadne]"
        return data
    except Exception:  # noqa: BLE001 - never ship unmasked data on a masking bug
        logger.warning("Ariadne mask failed; the field is dropped", exc_info=True)
        return "[omitted: masking failed]"

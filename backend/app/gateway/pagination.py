"""Shared pagination helpers for gateway routers."""

from __future__ import annotations

from deerflow.utils.messages import NO_RESPONSE_MARKER, is_blank_text


def mark_blank_final_ai_messages(messages: list[dict], last_ai_indices: set[int]) -> None:
    """[argus patch #37] In-place: replace a blank final assistant message with a
    visible marker so the web UI never renders an empty answer.

    local-qwen can end a run with an empty final AIMessage (no content / no
    tool_calls) after a long tool loop; patch #36 surfaces this on the channel
    delivery paths (Telegram/Slack), but the web UI reads persisted messages
    verbatim and shows blank — even after reload, because the empty content is
    what is stored. This is the web-side counterpart: a display-time safety net.

    Only the messages at ``last_ai_indices`` (the last ``ai_message`` per run,
    already computed by the caller for feedback attachment) are considered, so an
    empty *intermediate* assistant turn between tool calls is never touched. The
    deeper fix (retry-on-empty in the run loop) prevents most blanks upstream;
    this guarantees the ones that slip through are still visible.
    """
    for i in last_ai_indices:
        if 0 <= i < len(messages):
            msg = messages[i]
            if isinstance(msg, dict) and msg.get("event_type") == "ai_message" and is_blank_text(msg.get("content")):
                msg["content"] = NO_RESPONSE_MARKER


def trim_run_message_page(rows: list[dict], *, limit: int, after_seq: int | None) -> tuple[list[dict], bool]:
    """Trim a ``limit + 1`` run-message page while preserving page boundaries."""
    has_more = len(rows) > limit
    if not has_more:
        return rows, False

    if after_seq is not None:
        return rows[:limit], True

    return rows[-limit:], True

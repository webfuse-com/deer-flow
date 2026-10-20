"""Guards on the final reply a channel delivers for a run.

[argus patch #151] Closing-note recovery for unattended runs.

The channel manager delivers the text of the run's last AI message. A
scheduled run whose model wrote its content first and then ended on a short
closing note delivered only that note: atlas-dima's morning brief was written
in the AI message that also carried the topic-log ``write_file`` call, and
Telegram got "Brief delivered and topic log updated." (Chronos 42764 on
2026-10-13, 43020 on 10-14, 46257 on 10-20). When an unattended run's final
text is a closing note and an earlier AI message of the same turn carries
substantive text, the delivery is that text followed by the note.

The heuristic is deliberately narrow, all of these must hold:

- the final text is at most 160 characters on at most two lines;
- it refers to a delivery that already happened ("above", "delivered",
  "sent", "posted"), which the fleet's real short reports do not ("FDE
  trials: no change ... committed 080767a.", "Nothing pending today.");
- an earlier AI message after the turn's last visible human message has at
  least 200 characters of text and at least three times the note's length.

Nothing is dropped: the note follows the recovered content.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

_CLOSING_NOTE_MAX_CHARS = 160
_CLOSING_NOTE_MAX_LINES = 2
_CLOSING_NOTE_RE = re.compile(r"\b(?:above|delivered|sent|posted)\b", re.IGNORECASE)
SUBSTANTIVE_MIN_CHARS = 200
_SUBSTANTIVE_MIN_RATIO = 3


def _messages(result: Any) -> list[Any]:
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        messages = result.get("messages", [])
        if isinstance(messages, list):
            return messages
    return []


def _is_hidden_control_message(msg: Mapping[str, Any]) -> bool:
    """Same rule as the manager's: a human message hidden from the UI is not a turn boundary."""
    kwargs = msg.get("additional_kwargs")
    return isinstance(kwargs, Mapping) and kwargs.get("hide_from_ui") is True


def _ai_text(msg: Mapping[str, Any]) -> str:
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return ""


def _current_turn_ai_texts(result: Any) -> list[str]:
    """Non-empty AI texts of the current turn, newest first."""
    texts: list[str] = []
    for msg in reversed(_messages(result)):
        if not isinstance(msg, dict):
            continue
        msg_type = msg.get("type")
        if msg_type == "human":
            if _is_hidden_control_message(msg):
                continue
            break
        if msg_type == "ai":
            text = _ai_text(msg).strip()
            if text:
                texts.append(text)
    return texts


def _is_closing_note(note: str) -> bool:
    if not note or len(note) > _CLOSING_NOTE_MAX_CHARS:
        return False
    if sum(1 for line in note.splitlines() if line.strip()) > _CLOSING_NOTE_MAX_LINES:
        return False
    return _CLOSING_NOTE_RE.search(note) is not None


def _recover_closing_note_content(result: Any, response_text: str | None) -> str | None:
    """The text to deliver instead of a closing note, or None to deliver *response_text* as it is."""
    note = (response_text or "").strip()
    if not _is_closing_note(note):
        return None
    texts = _current_turn_ai_texts(result)
    # The note must be the turn's final AI text (not a clarification or a tool reply).
    if len(texts) < 2 or texts[0] != note:
        return None
    floor = max(SUBSTANTIVE_MIN_CHARS, _SUBSTANTIVE_MIN_RATIO * len(note))
    for text in texts[1:]:
        if len(text) >= floor:
            return f"{text}\n\n{note}"
    return None


def recover_unattended_reply(result: Any, response_text: str, *, thread_id: str) -> str:
    """[argus patch #151] *response_text*, or the content its closing note left behind plus the note."""
    recovered = _recover_closing_note_content(result, response_text)
    if recovered is None:
        return response_text
    logger.warning(
        "[Manager] unattended turn ended on a closing note; delivering the earlier content with it (thread_id=%s, note_len=%d, delivered_len=%d)",
        thread_id,
        len(response_text.strip()),
        len(recovered),
    )
    return recovered

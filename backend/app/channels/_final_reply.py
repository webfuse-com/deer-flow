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

[argus patch #152] Raw tool-call markup is never delivered as a reply.

A model whose tool call was not parsed can end the run with the call's text
as its answer: atlas-vipin's meeting-prep poll sent "<tool_call>
<|im_start|>function_calendar_list_events> ..." to Telegram at 23:51 on
2026-10-17 and Chronos recorded it as delivered. A call outside code blocks
is stripped from the final reply (and, on unattended runs, a reply that is
only a JSON tool-call object). An unattended reply with only trivial text
left is not sent and the run is reported failed; an interactive reply that
was nothing but markup becomes a one-line notice, any other is left as is.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping
from typing import Any, NamedTuple

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


# ---------------------------------------------------------------------------
# [argus patch #152] Raw tool-call markup

TOOL_CALL_MARKUP_ERROR = "final reply was raw tool-call markup (the model's tool call was not parsed); it was not delivered"
TOOL_CALL_MARKUP_NOTICE = "My reply came out as a raw tool call instead of an answer, so I did not send it. Please send your message again."

# A tool-call opener at the start of a line (at most three spaces in: four
# spaces or a tab make a markdown code block): Qwen/Hermes <tool_call>, Qwen's
# <function=name> (and the observed <functionfunction=name> slip), Claude-style
# <function_calls>/<invoke name=...>, chat-template and Kimi special tokens.
_TOOL_CALL_OPEN_RE = re.compile(
    r"^ {0,3}(?:<tool_call>|<function_calls>|(?P<call><invoke\s+name=|<function(?:function)?=[^>\n]*>)|<\|(?:im_start|tool_calls_section_begin|tool_call_begin)\|>)",
    re.MULTILINE | re.IGNORECASE,
)
_TOOL_CALL_CLOSE_RE = re.compile(r"</tool_call>|</function_calls>|</function>|<\|(?:im_end|tool_call_end|tool_calls_section_end)\|>", re.IGNORECASE)
_FENCE_RE = re.compile(r"^[ \t]*(?:```|~~~)")
# What follows an opener when it really is a call: a parameter or invoke tag, a
# Qwen function tag, Kimi's argument token, or a JSON object with a name and
# its arguments. Prose about the format ("<tool_call> wraps a JSON object")
# and chat-template explanations have none of these.
_CALL_SHAPED_RE = re.compile(
    r"<parameter[\s=>]|<invoke\s+name=|<function(?:function)?=|\|>function[\w=.-]*>|<\|tool_call_argument_begin\|>"
    r"|\"name\"\s*:\s*\"[^\"\n]{1,128}\"[^<]{0,4000}?\"(?:arguments|parameters)\"\s*:"
    r"|\"(?:arguments|parameters)\"\s*:[^<]{0,4000}?\"name\"\s*:",
    re.IGNORECASE,
)
_CALL_SCAN_CHARS = 4000
_BLANK_RUN_RE = re.compile(r"\n[ \t]*\n(?:[ \t]*\n)+")
_JSON_FENCE_RE = re.compile(r"```(?:json)?[ \t]*\n(.*?)\n?```", re.DOTALL)
_TOOL_NAME_RE = re.compile(r"[A-Za-z_][\w.:=-]{0,127}")
_ARGUMENT_KEYS = ("arguments", "parameters", "args", "input")
_CALL_KEYS = frozenset({"name", "id", "type", *_ARGUMENT_KEYS})


class MarkupDecision(NamedTuple):
    text: str
    found: bool
    refused: bool


def _segments(text: str) -> list[tuple[bool, str]]:
    """Split *text* into (fenced, segment) runs; an unclosed fence runs to the end."""
    segments: list[tuple[bool, str]] = []
    buffer: list[str] = []
    fenced = False
    for line in text.splitlines(keepends=True):
        if _FENCE_RE.match(line):
            if fenced:
                buffer.append(line)
                segments.append((True, "".join(buffer)))
                buffer, fenced = [], False
            else:
                segments.append((False, "".join(buffer)))
                buffer, fenced = [line], True
            continue
        buffer.append(line)
    segments.append((fenced, "".join(buffer)))
    return segments


def _strip_plain(segment: str) -> tuple[str, bool]:
    """Remove each call: an opener through its closer (and the closers right after it), or to the end.

    An opener counts only when it is a call: the opener names one itself
    (``<function=name>``, ``<invoke name=``), or what follows it up to its
    closer (or the end) is empty or call-shaped. Otherwise it is prose about
    the format and stays.
    """
    out: list[str] = []
    pos = 0
    found = False
    while (opener := _TOOL_CALL_OPEN_RE.search(segment, pos)) is not None:
        closer = _TOOL_CALL_CLOSE_RE.search(segment, opener.end())
        body = segment[opener.end() : closer.start() if closer is not None else len(segment)]
        if opener.group("call") is None and body.strip() and _CALL_SHAPED_RE.search(body[:_CALL_SCAN_CHARS]) is None:
            out.append(segment[pos : opener.end()])
            pos = opener.end()
            continue
        found = True
        out.append(segment[pos : opener.start()])
        if closer is None:
            pos = len(segment)
            break
        end = closer.end()
        while True:
            rest = segment[end:]
            following = _TOOL_CALL_CLOSE_RE.match(segment, end + len(rest) - len(rest.lstrip()))
            if following is None:
                break
            end = following.end()
        pos = end
    out.append(segment[pos:])
    return "".join(out), found


def _is_tool_call_object(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    calls = obj.get("tool_calls")
    if isinstance(calls, list) and calls and set(obj) <= {"tool_calls", "role", "content", "type"}:
        return all(_is_tool_call_object(call) for call in calls)
    function = obj.get("function")
    if isinstance(function, dict) and set(obj) <= {"function", "id", "type"}:
        obj = function
    name = obj.get("name")
    return isinstance(name, str) and _TOOL_NAME_RE.fullmatch(name) is not None and any(key in obj for key in _ARGUMENT_KEYS) and set(obj) <= _CALL_KEYS


def _is_tool_call_json(text: str) -> bool:
    body = text.strip()
    if (fence := _JSON_FENCE_RE.fullmatch(body)) is not None:
        body = fence.group(1).strip()
    if not body.startswith(("{", "[")):
        return False
    try:
        data = json.loads(body)
    except ValueError:
        return False
    items = data if isinstance(data, list) else [data]
    return bool(items) and all(_is_tool_call_object(item) for item in items)


def strip_tool_call_markup(text: str, *, unattended: bool) -> tuple[str, bool]:
    """Return (*text* without tool-call markup, whether any was found).

    Markup inside fenced code blocks and mentions inside a line are left
    alone. A reply that is only a JSON tool-call object counts as markup on
    unattended runs only (someone in a chat may have asked for that JSON).
    Without markup *text* comes back unchanged.
    """
    if not text:
        return text, False
    if unattended and _is_tool_call_json(text):
        return "", True
    parts: list[str] = []
    found = False
    for fenced, segment in _segments(text):
        if fenced:
            parts.append(segment)
            continue
        cleaned, hit = _strip_plain(segment)
        if hit:
            found = True
            cleaned = _BLANK_RUN_RE.sub("\n\n", cleaned)
        parts.append(cleaned)
    return ("".join(parts).strip(), True) if found else (text, False)


def reply_without_tool_call_markup(
    text: str,
    *,
    unattended: bool,
    has_artifacts: bool,
    thread_id: str,
    is_trivial: Callable[[str], bool] | None = None,
) -> MarkupDecision:
    """[argus patch #152] What to deliver once tool-call markup is removed from a final reply.

    Unattended: the remainder when it is not trivial on its own (*is_trivial*,
    the manager's filler and silence-announcement check), an empty text when
    files were presented, otherwise refused (the caller reports the run
    failed and sends nothing). Interactive: the reply exactly as written
    unless nothing but markup is left, then a one-line notice.
    """
    cleaned, found = strip_tool_call_markup(text, unattended=unattended)
    if not found:
        return MarkupDecision(text, False, False)
    trivial = is_trivial(cleaned) if is_trivial is not None else not cleaned.strip()
    if unattended:
        if trivial and not has_artifacts:
            logger.warning("[Manager] unattended final reply was raw tool-call markup; not delivering it (thread_id=%s, reply_len=%d)", thread_id, len(text))
            return MarkupDecision("", True, True)
        delivered = "" if trivial else cleaned
    elif cleaned:
        return MarkupDecision(text, False, False)
    else:
        delivered = "" if has_artifacts else TOOL_CALL_MARKUP_NOTICE
    logger.warning("[Manager] removed raw tool-call markup from the final reply (thread_id=%s, reply_len=%d, delivered_len=%d)", thread_id, len(text), len(delivered))
    return MarkupDecision(delivered, True, False)

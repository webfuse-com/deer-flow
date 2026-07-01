from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

ORIGINAL_USER_CONTENT_KEY = "original_user_content"

# [argus patch #37] Marker rendered in place of a blank final assistant turn so
# the answer does not silently vanish (web UI + channels + run-loop retry all
# share this).
NO_RESPONSE_MARKER = "(No response from agent)"


def is_blank_text(content: Any) -> bool:
    """[argus patch #37] True when *content* carries no deliverable text.

    Harness-level home for the emptiness check patch #36 introduced in
    ``app/channels/manager.py`` (which the harness may not import — the
    boundary is enforced by tests/test_harness_boundary.py). Both the web
    serialization guard and the run-loop retry-on-empty guard need it, so it
    lives here and the channel manager re-exports it.

    Accepts any LangChain message-content shape (str, or a list of text /
    dict blocks) via :func:`message_content_to_text`. Two failure modes make
    ``if not content`` insufficient:
      (a) whitespace-only — an empty final turn often leaves the last streamed
          partial as ``"\\n\\n"`` or ``" "``; that is truthy;
      (b) a short (<= 3 chars) non-alphanumeric filler — ``.``, ``-``, ``…``.
    """
    if content is None:
        return True
    stripped = message_content_to_text(content).strip()
    if not stripped:
        return True
    return len(stripped) <= 3 and re.search(r"\w", stripped) is None


def message_content_to_text(content: Any) -> str:
    """Extract text from LangChain message content shapes."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part for part in parts if part)
    return str(content)


def get_original_user_content_text(content: Any, additional_kwargs: Mapping[str, Any] | None) -> str:
    """Return pre-middleware user text when available, otherwise content text."""
    original_content = (additional_kwargs or {}).get(ORIGINAL_USER_CONTENT_KEY)
    if isinstance(original_content, str):
        return original_content
    return message_content_to_text(content)

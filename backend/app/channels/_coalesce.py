"""[argus patch #10] Debounced message coalescer.

Telegram (and other clients) split a long paste into several messages sent
within ~1s. Each arrives as its own InboundMessage; without coalescing the
manager runs them concurrently on the same thread and all but the first hit a
409 "thread already has an active run" and are LOST.

This coalescer buffers CHAT messages for the same conversation key and, after a
short quiet window (debounce), dispatches them as ONE combined message — so a
split paste becomes a single agent turn with a single reply. Commands are never
coalesced (the caller routes them straight through).

Keyed by (channel_name, chat_id, topic_id) — the same tuple the manager maps to
a DeerFlow thread, so a split paste (same chat + topic) buffers together and
lands on the one thread it would have anyway.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.channels.message_bus import InboundMessage

logger = logging.getLogger(__name__)

DEFAULT_COALESCE_WINDOW = 2.5  # seconds of quiet before a burst is dispatched

# dispatch(combined_message) -> awaitable
Dispatch = Callable[[InboundMessage], Awaitable[None]]


def _key(msg: InboundMessage) -> tuple[str, str, str | None]:
    return (msg.channel_name, msg.chat_id, msg.topic_id)


def combine_messages(msgs: list[InboundMessage]) -> InboundMessage:
    """Merge a buffered burst into one InboundMessage. Texts joined in arrival
    order with blank lines; the first message donates identity (chat/user/
    topic/thread_ts/type); file attachments from all parts are concatenated."""
    if len(msgs) == 1:
        return msgs[0]
    head = msgs[0]
    text = "\n\n".join(m.text for m in msgs if m.text)
    files: list = []
    for m in msgs:
        if m.files:
            files.extend(m.files)
    return InboundMessage(
        channel_name=head.channel_name,
        chat_id=head.chat_id,
        user_id=head.user_id,
        text=text,
        msg_type=head.msg_type,
        thread_ts=head.thread_ts,
        topic_id=head.topic_id,
        files=files,
        metadata=head.metadata,
    )


@dataclass
class _Pending:
    buffer: list[InboundMessage] = field(default_factory=list)
    timer: asyncio.Task | None = None


class MessageCoalescer:
    """Per-conversation debounced buffer. Call ``add(msg)`` for each CHAT
    message; the coalescer fires ``dispatch(combined)`` once a conversation has
    been quiet for ``window`` seconds."""

    def __init__(self, dispatch: Dispatch, *, window: float = DEFAULT_COALESCE_WINDOW) -> None:
        self._dispatch = dispatch
        self._window = window
        self._pending: dict[tuple[str, str, str | None], _Pending] = {}

    def add(self, msg: InboundMessage) -> None:
        """Buffer a message and (re)arm its conversation's debounce timer."""
        k = _key(msg)
        pending = self._pending.get(k)
        if pending is None:
            pending = _Pending()
            self._pending[k] = pending
        pending.buffer.append(msg)
        if pending.timer and not pending.timer.done():
            pending.timer.cancel()
        pending.timer = asyncio.ensure_future(self._fire_after_quiet(k))

    async def _fire_after_quiet(self, k: tuple[str, str, str | None]) -> None:
        try:
            await asyncio.sleep(self._window)
        except asyncio.CancelledError:
            return  # a newer message reset the window
        pending = self._pending.pop(k, None)
        if not pending or not pending.buffer:
            return
        combined = combine_messages(pending.buffer)
        if len(pending.buffer) > 1:
            logger.info(
                "[Coalesce] merged %d messages for chat=%s topic=%s into one turn",
                len(pending.buffer),
                k[1],
                k[2],
            )
        await self._dispatch(combined)

    async def flush(self) -> None:
        """Dispatch any buffered messages immediately (e.g. on shutdown)."""
        keys = list(self._pending.keys())
        for k in keys:
            pending = self._pending.pop(k, None)
            if not pending:
                continue
            if pending.timer and not pending.timer.done():
                pending.timer.cancel()
            if pending.buffer:
                await self._dispatch(combine_messages(pending.buffer))

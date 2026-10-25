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
from dataclasses import dataclass, field, replace

from app.channels.message_bus import InboundMessage

logger = logging.getLogger(__name__)

DEFAULT_COALESCE_WINDOW = 0.8  # seconds of quiet before a burst is dispatched.
# 0.8s comfortably covers a real Telegram split-paste: an 18-chunk paste on
# 2026-05-29 had a max inter-chunk gap of ~0.44s. Keeps the per-turn latency
# tax low on normal single messages. Override via channels.coalesce_window.

# dispatch(combined_message) -> awaitable; with ``pass_parts=True``,
# dispatch(combined_message, parts) so the caller can settle every part's
# dedupe key (the manager does).
Dispatch = Callable[..., Awaitable[None]]


def _key(msg: InboundMessage) -> tuple[str, str, str | None]:
    return (msg.channel_name, msg.chat_id, msg.topic_id)


def combine_messages(msgs: list[InboundMessage]) -> InboundMessage:
    """Merge a buffered burst into one InboundMessage. Texts joined in arrival
    order with blank lines; the first message donates everything else (chat,
    user, topic, thread_ts, type, connection and owner, metadata and so the
    dedupe and idempotency key, the playbook fields); file attachments from
    all parts are concatenated."""
    if len(msgs) == 1:
        return msgs[0]
    head = msgs[0]
    text = "\n\n".join(m.text for m in msgs if m.text)
    files: list = []
    for m in msgs:
        if m.files:
            files.extend(m.files)
    # replace() keeps every field of the head; building a fresh message lost
    # the connection, owner and playbook fields (unattended, report_url).
    return replace(head, text=text, files=files)


@dataclass
class _Pending:
    buffer: list[InboundMessage] = field(default_factory=list)
    timer: asyncio.Task | None = None


class MessageCoalescer:
    """Per-conversation debounced buffer. Call ``add(msg)`` for each CHAT
    message; the coalescer fires ``dispatch(combined)`` once a conversation has
    been quiet for ``window`` seconds."""

    def __init__(self, dispatch: Dispatch, *, window: float = DEFAULT_COALESCE_WINDOW, pass_parts: bool = False) -> None:
        self._dispatch = dispatch
        self._window = window
        self._pass_parts = pass_parts
        self._pending: dict[tuple[str, str, str | None], _Pending] = {}
        # Debounce timers and the bursts they dispatch, so shutdown can wait
        # for a running burst and cancel it like a worker (flush_nowait).
        self._tasks: set[asyncio.Task] = set()

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

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
        pending.timer = self._spawn(self._fire_after_quiet(k))

    async def _fire_after_quiet(self, k: tuple[str, str, str | None]) -> None:
        try:
            await asyncio.sleep(self._window)
        except asyncio.CancelledError:
            return  # a newer message reset the window
        pending = self._pending.pop(k, None)
        if not pending or not pending.buffer:
            return
        if len(pending.buffer) > 1:
            logger.info(
                "[Coalesce] merged %d messages for chat=%s topic=%s into one turn",
                len(pending.buffer),
                k[1],
                k[2],
            )
        await self._dispatch_buffer(pending.buffer)

    async def _dispatch_buffer(self, buffer: list[InboundMessage]) -> None:
        combined = combine_messages(buffer)
        if self._pass_parts:
            await self._dispatch(combined, list(buffer))
        else:
            await self._dispatch(combined)

    def flush_nowait(self) -> set[asyncio.Task]:
        """Start every buffered burst now and return the tasks still running.

        For shutdown: the caller waits for the returned tasks within its grace
        period and cancels what is left, as it does with its workers.
        """
        for k in list(self._pending):
            pending = self._pending.pop(k)
            if pending.timer and not pending.timer.done():
                pending.timer.cancel()
            if pending.buffer:
                self._spawn(self._dispatch_buffer(pending.buffer))
        return {task for task in self._tasks if not task.done()}

    async def flush(self) -> None:
        """Dispatch any buffered messages immediately, inline."""
        keys = list(self._pending.keys())
        for k in keys:
            pending = self._pending.pop(k, None)
            if not pending:
                continue
            if pending.timer and not pending.timer.done():
                pending.timer.cancel()
            if pending.buffer:
                await self._dispatch_buffer(pending.buffer)

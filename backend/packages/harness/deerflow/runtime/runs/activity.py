"""[argus patch #103] Thread activity: who started or finished a run, for open tabs.

A browser tab streams only the runs it starts itself (or one it finds active
when it loads). A run started anywhere else (a build wake-up from the host, a
Telegram message, a scheduled playbook) never reached a tab that already had
the thread open: the reply appeared only after a reload.

The run manager publishes one small event whenever a run is admitted or
changes status; ``GET /api/threads/activity`` streams those to the browser,
filtered to the viewer's own threads. The tab then refetches the thread's run
list, and the existing rejoin (``joinStream``) streams the reply live.

In-process on purpose: a stack runs one gateway worker (``GATEWAY_WORKERS=1``),
so every run passes through this process. Events carry no content (thread,
run, status, owner) and are never replayed: a client that connects or
reconnects refetches, because the data is already durable.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Per subscriber. A tab that falls this far behind gets one "resync" instead.
QUEUE_SIZE = 256


class RunActivityHub:
    """Fan run activity out to subscribers on the gateway's event loop."""

    def __init__(self, queue_size: int = QUEUE_SIZE) -> None:
        self._queue_size = queue_size
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, event: dict[str, Any]) -> None:
        """Never blocks and never raises into the run path."""
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Too slow to keep up: drop its backlog and tell it to refetch.
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait({"type": "resync"})
            except Exception:  # noqa: BLE001 - activity is best effort
                logger.debug("thread activity publish failed", exc_info=True)


hub = RunActivityHub()


def publish_run(record: Any) -> None:
    """Publish one run's current status (from the run manager)."""
    try:
        status = getattr(record, "status", None)
        hub.publish(
            {
                "type": "run",
                "thread_id": record.thread_id,
                "run_id": record.run_id,
                "status": getattr(status, "value", status),
                "user_id": getattr(record, "user_id", None),
                "updated_at": getattr(record, "updated_at", None),
            }
        )
    except Exception:  # noqa: BLE001 - activity is best effort
        logger.debug("thread activity event dropped", exc_info=True)

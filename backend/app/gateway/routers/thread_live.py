"""[argus patch #103] ``GET /api/threads/activity``: run activity for open tabs.

One Server-Sent Events stream per open workspace tab. It carries a small event
whenever a run on one of the viewer's threads is admitted or changes status,
whoever started it (the tab itself, a build wake-up, Telegram, a schedule),
plus a heartbeat. The frontend then refetches that thread's run list and joins
the run with the existing rejoin, so the reply streams in live.

Events name the thread, the run and the status, never content. A run is the
viewer's when its row is stamped with their id or its thread is theirs. Nothing
is replayed: on (re)connect the client refetches what it shows.

Registered before every ``/api/threads/{thread_id}`` route so ``/activity`` is
not read as a thread id (``test_activity_is_routed_before_any_thread_id_route``).
Upstream's own ``routers/thread_activity.py`` (``/api/thread-activity``) is a
different feed: polled, server-started runs only, for the sidebar.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.gateway.authz import require_permission
from app.gateway.deps import get_current_user, get_thread_store
from app.gateway.sse_headers import sse_response_headers
from deerflow.runtime.runs.live_activity import hub

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/threads", tags=["threads"])

# Under nginx's 600 s read timeout on /api/threads, and short enough that a
# closed tab releases its subscription quickly.
HEARTBEAT_SECONDS = 25.0
# Thread owners looked up per connection; a tab follows a few threads at most.
OWNER_CACHE_SIZE = 512


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


async def _thread_owner(request: Request, thread_id: str, cache: dict[str, str | None]) -> str | None:
    if thread_id in cache:
        return cache[thread_id]
    owner: str | None = None
    try:
        meta = await get_thread_store(request).get(thread_id, user_id=None)
        owner = meta.get("user_id") if isinstance(meta, dict) else getattr(meta, "user_id", None)
    except Exception:  # noqa: BLE001 - an unknown owner is simply not shown
        logger.debug("thread activity: owner lookup failed for %s", thread_id, exc_info=True)
    if len(cache) >= OWNER_CACHE_SIZE:
        cache.pop(next(iter(cache)))
    cache[thread_id] = str(owner) if owner else None
    return cache[thread_id]


async def is_visible(event: dict, viewer: str | None, owner_of) -> bool:
    """A run is the viewer's when its row carries their id or its thread is theirs."""
    if not viewer:
        return False
    if event.get("user_id") and str(event["user_id"]) == viewer:
        return True
    return await owner_of(str(event.get("thread_id") or "")) == viewer


@router.get("/activity")
@require_permission("threads", "read")
async def thread_live_activity(request: Request) -> StreamingResponse:
    """Stream run activity on the viewer's threads (Server-Sent Events)."""
    viewer = await get_current_user(request)
    owners: dict[str, str | None] = {}

    async def owner_of(thread_id: str) -> str | None:
        return await _thread_owner(request, thread_id, owners)

    async def stream():
        # Subscribed inside the generator, so a stream that never starts
        # never leaves a subscription behind.
        queue = hub.subscribe()
        try:
            yield _sse("ready", {"heartbeat_seconds": HEARTBEAT_SECONDS})
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except TimeoutError:
                    if await request.is_disconnected():
                        return
                    yield ": ping\n\n"
                    continue
                if event.get("type") == "resync":
                    yield _sse("resync", {})
                    continue
                if not await is_visible(event, viewer, owner_of):
                    continue
                yield _sse("run", {k: event.get(k) for k in ("thread_id", "run_id", "status", "updated_at", "origin_kind")})
        finally:
            hub.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers=sse_response_headers(),
    )

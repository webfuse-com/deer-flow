"""[argus patch #103] Live run activity: open tabs learn about runs they did not start."""

import pytest

from app.gateway.routers import thread_live
from deerflow.runtime.runs.live_activity import RunActivityHub, hub, publish_run
from deerflow.runtime.runs.manager import RunManager
from deerflow.runtime.runs.schemas import RunStatus


def test_publish_reaches_every_subscriber_and_unsubscribe_stops_it():
    h = RunActivityHub()
    a, b = h.subscribe(), h.subscribe()
    h.publish({"type": "run", "run_id": "r1"})
    assert a.get_nowait()["run_id"] == "r1" and b.get_nowait()["run_id"] == "r1"
    h.unsubscribe(a)
    h.publish({"type": "run", "run_id": "r2"})
    assert a.empty() and b.get_nowait()["run_id"] == "r2"
    assert h.subscriber_count == 1


def test_a_slow_subscriber_gets_one_resync_instead_of_blocking_the_run():
    h = RunActivityHub(queue_size=2)
    q = h.subscribe()
    for i in range(5):
        h.publish({"type": "run", "run_id": f"r{i}"})  # never raises, never blocks
    events = [q.get_nowait() for _ in range(q.qsize())]
    assert {"type": "resync"} in events and len(events) <= 2


@pytest.mark.asyncio
async def test_the_run_manager_publishes_admission_and_every_status_change():
    q = hub.subscribe()
    try:
        mgr = RunManager()
        record = await mgr.create_or_reject("thread-a", user_id="u-1")
        await mgr.set_status(record.run_id, RunStatus.running)
        await mgr.set_status(record.run_id, RunStatus.success)
        seen = []
        while not q.empty():
            ev = q.get_nowait()
            if ev.get("run_id") == record.run_id:
                seen.append(ev["status"])
        assert seen == ["pending", "running", "success"]
    finally:
        hub.unsubscribe(q)


def test_publish_run_carries_no_content():
    class Rec:
        thread_id, run_id, user_id, updated_at = "t", "r", "u", "now"
        status = RunStatus.running
        kwargs = {"input": {"messages": ["secret"]}}
        metadata = {"deerflow_origin": {"kind": "schedule"}}

    q = hub.subscribe()
    try:
        publish_run(Rec())
        ev = q.get_nowait()
        assert set(ev) == {"type", "thread_id", "run_id", "status", "user_id", "updated_at", "origin_kind"}
        assert ev["status"] == "running"
    finally:
        hub.unsubscribe(q)


@pytest.mark.asyncio
async def test_only_the_viewers_runs_and_threads_are_shown():
    owners = {"mine": "u-1", "theirs": "u-2"}

    async def owner_of(thread_id):
        return owners.get(thread_id)

    vis = thread_live.is_visible
    assert await vis({"thread_id": "theirs", "user_id": "u-1"}, "u-1", owner_of)  # stamped with my id
    assert await vis({"thread_id": "mine", "user_id": "default"}, "u-1", owner_of)  # my thread, e.g. a schedule
    assert not await vis({"thread_id": "theirs", "user_id": "u-2"}, "u-1", owner_of)  # someone else's
    assert not await vis({"thread_id": "unknown", "user_id": None}, "u-1", owner_of)
    assert not await vis({"thread_id": "mine", "user_id": "u-1"}, None, owner_of)  # no viewer, nothing


def test_activity_is_routed_before_any_thread_id_route():
    from app.gateway.app import create_app

    paths = [getattr(r, "path", "") for r in create_app().routes]
    activity = paths.index("/api/threads/activity")
    # Only a one-segment route can swallow "/api/threads/activity".
    first_thread_route = min(i for i, p in enumerate(paths) if p == "/api/threads/{thread_id}")
    assert activity < first_thread_route


def test_origin_kind_marks_server_started_runs():
    """Interactive runs carry None; the browser leaves server-started ones' sidebar refresh to upstream's feed."""
    from deerflow.runtime.run_origin import ORIGIN_KINDS

    kind = sorted(ORIGIN_KINDS)[0]

    class Rec:
        thread_id, run_id, user_id, updated_at = "t", "r", "u", "now"
        status = RunStatus.pending
        metadata: dict = {}

    q = hub.subscribe()
    try:
        publish_run(Rec())
        assert q.get_nowait()["origin_kind"] is None
        Rec.metadata = {"deerflow_origin": {"kind": kind}}
        publish_run(Rec())
        assert q.get_nowait()["origin_kind"] == kind
    finally:
        hub.unsubscribe(q)


def test_upstream_sidebar_feed_keeps_its_own_route():
    from app.gateway.app import create_app

    paths = {getattr(r, "path", "") for r in create_app().routes}
    assert "/api/threads/activity" in paths  # ours: live SSE
    assert "/api/thread-activity" in paths  # upstream's: polled sidebar feed

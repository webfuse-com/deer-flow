"""[argus patch #10] Tests for the split-paste message coalescer."""

from __future__ import annotations

import asyncio

from app.channels._coalesce import MessageCoalescer, combine_messages
from app.channels.message_bus import InboundMessage, InboundMessageType


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _msg(text, *, chat="c1", topic=None, channel="telegram", mtype=InboundMessageType.CHAT, files=None):
    return InboundMessage(
        channel_name=channel,
        chat_id=chat,
        user_id="u1",
        text=text,
        msg_type=mtype,
        topic_id=topic,
        files=files or [],
    )


def test_burst_within_window_becomes_one():
    async def go():
        got = []
        coal = MessageCoalescer(lambda m: _collect(got, m), window=0.05)
        coal.add(_msg("part A"))
        await asyncio.sleep(0.01)
        coal.add(_msg("part B"))
        await asyncio.sleep(0.01)
        coal.add(_msg("part C"))
        await asyncio.sleep(0.15)  # let the window expire
        assert len(got) == 1
        assert got[0].text == "part A\n\npart B\n\npart C"

    _run(go())


def test_messages_spaced_apart_stay_separate():
    async def go():
        got = []
        coal = MessageCoalescer(lambda m: _collect(got, m), window=0.05)
        coal.add(_msg("first"))
        await asyncio.sleep(0.12)  # window expires → dispatch 1
        coal.add(_msg("second"))
        await asyncio.sleep(0.12)  # window expires → dispatch 2
        assert [m.text for m in got] == ["first", "second"]

    _run(go())


def test_different_conversations_do_not_cross():
    async def go():
        got = []
        coal = MessageCoalescer(lambda m: _collect(got, m), window=0.05)
        coal.add(_msg("a", chat="c1"))
        coal.add(_msg("b", chat="c2"))
        await asyncio.sleep(0.15)
        texts = sorted(m.text for m in got)
        assert texts == ["a", "b"]  # two separate dispatches, not merged

    _run(go())


def test_different_topics_do_not_cross():
    async def go():
        got = []
        coal = MessageCoalescer(lambda m: _collect(got, m), window=0.05)
        coal.add(_msg("x", topic="t1"))
        coal.add(_msg("y", topic="t2"))
        await asyncio.sleep(0.15)
        assert sorted(m.text for m in got) == ["x", "y"]

    _run(go())


def test_combine_preserves_identity_and_files():
    m1 = _msg("A", files=[{"path": "/a"}])
    m2 = _msg("B", files=[{"path": "/b"}])
    combined = combine_messages([m1, m2])
    assert combined.text == "A\n\nB"
    assert combined.chat_id == "c1"
    assert [f["path"] for f in combined.files] == ["/a", "/b"]


def test_combine_single_is_passthrough():
    m = _msg("solo")
    assert combine_messages([m]) is m


def test_flush_dispatches_buffer():
    async def go():
        got = []
        coal = MessageCoalescer(lambda m: _collect(got, m), window=100)  # never auto-fire
        coal.add(_msg("buffered"))
        await coal.flush()
        assert len(got) == 1 and got[0].text == "buffered"

    _run(go())


async def _collect(sink, msg):
    sink.append(msg)


# -- sync 2026-10-25: the coalescer under upstream's dedupe claims and run admission (#7104) --

import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest.mock import AsyncMock, MagicMock  # noqa: E402

from app.channels.dedupe_store import MemoryInboundDedupeStore  # noqa: E402
from app.channels.message_bus import INBOUND_FILE_CLEANUP_KEY, MessageBus  # noqa: E402
from app.channels.store import JsonChannelStore  # noqa: E402


def _slack(text, message_id, **kwargs):
    # Slack is a non-streaming channel (runs.wait); team_id makes the message keyed.
    return InboundMessage(channel_name="slack", chat_id="C1", user_id="U1", text=text, metadata={"message_id": message_id, "team_id": "T1"}, **kwargs)


def _manager(dedupe, *, grace=5.0):
    from app.channels.manager import ChannelManager

    bus = MessageBus()
    manager = ChannelManager(
        bus=bus,
        store=JsonChannelStore(path=Path(tempfile.mkdtemp()) / "store.json"),
        inbound_dedupe_store=dedupe,
        coalesce_window=0.05,
        shutdown_grace_period_seconds=grace,
    )
    client = MagicMock()
    client.threads.create = AsyncMock(return_value={"thread_id": "t1"})
    client.threads.get = AsyncMock(return_value={"thread_id": "t1", "metadata": {}})
    client.threads.update = AsyncMock(return_value={})
    client.runs.wait = AsyncMock(return_value={"messages": [{"type": "human", "content": "x"}, {"type": "ai", "content": "Answer"}]})
    manager._client = client
    outbound = []

    async def capture(msg):
        outbound.append(msg)

    bus.subscribe_outbound(capture)
    return manager, bus, client, outbound


async def _until(condition, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError
        await asyncio.sleep(0.01)


def _keys(*msgs):
    from app.channels.manager import ChannelManager

    return [ChannelManager._inbound_dedupe_key(m) for m in msgs]


def test_merged_burst_whose_run_ran_keeps_every_parts_dedupe_key():
    """An admitted run commits the whole burst: a redelivery of any part stays a duplicate."""

    async def go():
        from app.channels.manager import IDEMPOTENCY_KEY_HEADER, ChannelManager

        dedupe = MemoryInboundDedupeStore()
        manager, bus, client, outbound = _manager(dedupe)
        a, b = _slack("part A", "m1"), _slack("part B", "m2")
        await manager.start()
        await bus.publish_inbound(a)
        await bus.publish_inbound(b)
        await _until(lambda: any(m.is_final for m in outbound))
        await manager.stop()

        assert client.runs.wait.await_count == 1
        kwargs = client.runs.wait.await_args.kwargs
        assert kwargs["input"]["messages"][0]["content"] == "part A\n\npart B"
        assert kwargs["headers"][IDEMPOTENCY_KEY_HEADER] == ChannelManager._run_idempotency_key(a)
        assert all(key in dedupe._store for key in _keys(a, b))
        assert manager._coalesced_dedupe_keys == {}

    _run(go())


def test_merged_burst_failing_before_admission_releases_every_parts_dedupe_key():
    """Nothing reached the Gateway: a redelivery of any part must be able to retry."""

    async def go():
        dedupe = MemoryInboundDedupeStore()
        manager, bus, client, outbound = _manager(dedupe)
        client.threads.create = AsyncMock(side_effect=RuntimeError("gateway down"))
        a, b = _slack("part A", "m1"), _slack("part B", "m2")
        await manager.start()
        await bus.publish_inbound(a)
        await bus.publish_inbound(b)
        await _until(lambda: any("internal error" in (m.text or "") for m in outbound))
        await manager.stop()

        assert client.runs.wait.await_count == 0
        assert not any(key in dedupe._store for key in _keys(a, b))

    _run(go())


def test_merged_burst_rejected_as_busy_releases_every_parts_dedupe_key():
    """A release site inside the run path (thread busy) releases the whole burst too."""

    async def go():
        import httpx
        from langgraph_sdk.errors import ConflictError

        dedupe = MemoryInboundDedupeStore()
        manager, bus, client, outbound = _manager(dedupe)
        busy = ConflictError("thread busy", response=httpx.Response(409, request=httpx.Request("POST", "http://gw/runs/wait")), body=None)
        client.runs.wait = AsyncMock(side_effect=busy)
        a, b = _slack("part A", "m1"), _slack("part B", "m2")
        await manager.start()
        await bus.publish_inbound(a)
        await bus.publish_inbound(b)
        await _until(lambda: any(m.text for m in outbound))
        await manager.stop()

        assert not any(key in dedupe._store for key in _keys(a, b))

    _run(go())


def test_stop_cancels_an_in_flight_burst_and_gives_back_its_keys():
    """A burst running at shutdown shares the workers' grace period and is cancelled
    with them; cancelled before its run request, it gives every part's key back."""

    async def go():
        dedupe = MemoryInboundDedupeStore()
        manager, bus, client, outbound = _manager(dedupe, grace=0)
        started = asyncio.Event()

        async def block(**kwargs):
            started.set()
            await asyncio.Event().wait()

        client.threads.create = AsyncMock(side_effect=block)
        a, b = _slack("part A", "m1"), _slack("part B", "m2")
        await manager.start()
        await bus.publish_inbound(a)
        await bus.publish_inbound(b)
        await asyncio.wait_for(started.wait(), timeout=5)
        await asyncio.wait_for(manager.stop(), timeout=5)

        assert not any(key in dedupe._store for key in _keys(a, b))
        assert client.runs.wait.await_count == 0

    _run(go())


def test_a_parts_attachments_are_kept_until_the_merged_turn_ingests_them():
    """The worker hands a coalesced message's staged files to the merged turn;
    cleaning them up when the worker moved on deleted them before ingestion."""

    async def go():
        dedupe = MemoryInboundDedupeStore()
        manager, bus, client, outbound = _manager(dedupe)
        cleaned = []
        seen_at_dispatch = []
        original = manager._handle_message

        async def spy(msg):
            seen_at_dispatch.append(list(cleaned))
            await original(msg)

        manager._handle_message = spy
        a = _slack("part A", "m1", files=[{"filename": "a.txt", INBOUND_FILE_CLEANUP_KEY: lambda: cleaned.append("a")}])
        b = _slack("part B", "m2")
        await manager.start()
        await bus.publish_inbound(a)
        await bus.publish_inbound(b)
        await _until(lambda: bool(seen_at_dispatch) and bool(cleaned))
        await manager.stop()

        assert seen_at_dispatch == [[]]
        assert cleaned == ["a"]

    _run(go())


def test_combine_keeps_the_heads_routing_and_playbook_fields():
    head = InboundMessage(
        channel_name="telegram",
        chat_id="c1",
        user_id="u1",
        text="A",
        connection_id="conn-1",
        owner_user_id="owner-1",
        workspace_id="w1",
        metadata={"message_id": "1"},
        agent_name="atlas",
        unattended=True,
        memory_mode="off",
        allowed_tools=["bash"],
        report_url="http://argus-scheduler:8000/api/runs/7/output",
        dedupe_claimed=True,
    )
    combined = combine_messages([head, _msg("B")])
    assert combined.text == "A\n\nB"
    for name in ("connection_id", "owner_user_id", "workspace_id", "metadata", "agent_name", "unattended", "memory_mode", "allowed_tools", "report_url", "dedupe_claimed", "created_at"):
        assert getattr(combined, name) == getattr(head, name), name

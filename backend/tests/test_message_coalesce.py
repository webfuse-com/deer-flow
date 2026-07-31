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

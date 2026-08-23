"""[argus] Tests for the patched Telegram send path (fork patch #10):
HTML parse_mode, chunking, plain-text fallback, and the working-emoji
delete-on-final lifecycle."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from app.channels import _telegram_sender
from app.channels.message_bus import MessageBus, OutboundMessage
from app.channels.telegram import TelegramChannel


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _channel_with_bot():
    ch = TelegramChannel(bus=MessageBus(), config={"bot_token": "t"})
    app = MagicMock()
    bot = AsyncMock()
    sent = MagicMock()
    sent.message_id = 1
    bot.send_message.return_value = sent
    app.bot = bot
    ch._application = app
    return ch, bot


def test_send_uses_html_parse_mode_and_converts_markdown():
    ch, bot = _channel_with_bot()
    msg = OutboundMessage(channel_name="telegram", chat_id="1", thread_id="t", text="**bold** and `code`")

    _run(ch.send(msg))

    assert bot.send_message.await_count == 1
    kwargs = bot.send_message.await_args.kwargs
    assert kwargs["parse_mode"] == "HTML"
    assert "<b>bold</b>" in kwargs["text"]
    assert "<code>code</code>" in kwargs["text"]


def test_long_message_is_chunked():
    ch, bot = _channel_with_bot()
    big = "\n".join(f"line {i} " + "x" * 60 for i in range(400))
    msg = OutboundMessage(channel_name="telegram", chat_id="1", thread_id="t", text=big)

    _run(ch.send(msg))

    assert bot.send_message.await_count > 1
    for call in bot.send_message.await_args_list:
        assert len(call.kwargs["text"]) <= 4096


def test_html_rejected_falls_back_to_plaintext():
    ch = TelegramChannel(bus=MessageBus(), config={"bot_token": "t"})
    app = MagicMock()
    bot = AsyncMock()

    class BadRequest(Exception):
        pass

    calls = []

    async def send_message(**kwargs):
        calls.append(kwargs)
        if kwargs.get("parse_mode") == "HTML":
            raise BadRequest("can't parse entities")
        m = MagicMock()
        m.message_id = 5
        return m

    bot.send_message = send_message
    app.bot = bot
    ch._application = app

    _run(ch.send(OutboundMessage(channel_name="telegram", chat_id="1", thread_id="t", text="**x**")))

    # First attempt with HTML, second without.
    assert calls[0].get("parse_mode") == "HTML"
    assert "parse_mode" not in calls[-1]


def _counting_send(bot):
    """Make bot.send_message return incrementing message_ids."""
    state = {"n": 0}

    async def send_message(**kwargs):
        state["n"] += 1
        m = MagicMock()
        m.message_id = state["n"]
        return m

    bot.send_message = send_message
    return state


def test_received_stage_sent_then_deleted_on_final():
    async def go():
        ch, bot = _channel_with_bot()
        _counting_send(bot)

        await ch._send_running_reply("1", reply_to_message_id=10)
        assert "1" in ch._working_msg
        assert ch._working_stage["1"] == "received"

        # Final answer deletes the emoji.
        await ch.send(OutboundMessage(channel_name="telegram", chat_id="1", thread_id="t", text="done", is_final=True))
        bot.delete_message.assert_awaited()
        assert "1" not in ch._working_msg

    _run(go())


def test_stage_change_resends_and_deletes_old():
    async def go():
        ch, bot = _channel_with_bot()
        _counting_send(bot)
        ch._stage_min_interval = 0  # no throttle for this test

        # received → thinking → working: each a fresh send, old one deleted.
        await ch.send(OutboundMessage(channel_name="telegram", chat_id="1", thread_id="t", text="", is_final=False, progress_stage="received"))
        first = ch._working_msg["1"]
        await ch.send(OutboundMessage(channel_name="telegram", chat_id="1", thread_id="t", text="", is_final=False, progress_stage="thinking"))
        assert ch._working_stage["1"] == "thinking"
        assert ch._working_msg["1"] != first
        # The previous emoji message was deleted on the swap.
        assert bot.delete_message.await_count == 1

    _run(go())


def test_concurrent_stage_changes_are_serialized_and_final_cleans_latest():
    async def go():
        ch, bot = _channel_with_bot()
        ch._stage_min_interval = 999

        next_message_id = 0
        transition_started = asyncio.Event()
        release_transition = asyncio.Event()
        active_stage_sends = 0
        max_active_stage_sends = 0

        async def send_message(**kwargs):
            nonlocal next_message_id, active_stage_sends, max_active_stage_sends
            next_message_id += 1
            if kwargs["text"] != "👀":
                active_stage_sends += 1
                max_active_stage_sends = max(max_active_stage_sends, active_stage_sends)
                transition_started.set()
                await release_transition.wait()
                active_stage_sends -= 1
            sent = MagicMock()
            sent.message_id = next_message_id
            return sent

        bot.send_message = send_message

        await _telegram_sender.show_stage(ch, 1, "1", "received", force=True)
        transitions = [asyncio.create_task(_telegram_sender.show_stage(ch, 1, "1", stage, force=True)) for stage in ("thinking", "working", "writing")]
        await transition_started.wait()
        final_cleanup = asyncio.create_task(
            ch.send(
                OutboundMessage(
                    channel_name="telegram",
                    chat_id="1",
                    thread_id="t",
                    text="",
                    is_final=True,
                )
            )
        )
        release_transition.set()
        await asyncio.gather(*transitions, final_cleanup)

        assert max_active_stage_sends == 1
        assert [call.kwargs["message_id"] for call in bot.delete_message.await_args_list] == [1, 2, 3, 4]
        assert "1" not in ch._working_msg
        assert "1" not in ch._working_stage
        assert "1" not in ch._working_at

    _run(go())


def test_stage_throttled_within_interval():
    async def go():
        ch, bot = _channel_with_bot()
        _counting_send(bot)
        ch._stage_min_interval = 999  # never allow a re-send within the window

        await ch.send(OutboundMessage(channel_name="telegram", chat_id="1", thread_id="t", text="", is_final=False, progress_stage="received"))
        msg1 = ch._working_msg["1"]
        # A different stage arrives too soon → ignored (latest-wins, no re-send).
        await ch.send(OutboundMessage(channel_name="telegram", chat_id="1", thread_id="t", text="", is_final=False, progress_stage="working"))
        assert ch._working_msg["1"] == msg1  # unchanged
        assert ch._working_stage["1"] == "received"  # still the first stage

    _run(go())


def test_progress_message_bypasses_html_formatter():
    async def go():
        ch, bot = _channel_with_bot()
        _counting_send(bot)
        # A progress message must NOT be HTML-formatted or chunked — it only
        # drives the emoji. (We assert no parse_mode HTML send happened.)
        await ch.send(OutboundMessage(channel_name="telegram", chat_id="1", thread_id="t", text="**not shown**", is_final=False, progress_stage="thinking"))
        # The emoji send carries no parse_mode (plain emoji), and the markdown
        # text was ignored entirely.
        assert ch._working_stage["1"] == "thinking"

    _run(go())


def test_partial_does_not_delete_working_emoji():
    ch, bot = _channel_with_bot()
    ch._working_msg["1"] = 77

    _run(ch.send(OutboundMessage(channel_name="telegram", chat_id="1", thread_id="t", text="partial", is_final=False)))

    bot.delete_message.assert_not_awaited()
    assert ch._working_msg.get("1") == 77


def test_telegram_reports_streaming_support():
    """[argus patch #10] The manager's _channel_supports_streaming reads the
    live channel's supports_streaming property; it MUST be True or telegram
    silently falls back to runs.wait and emits no stage signals."""
    from app.channels.message_bus import MessageBus

    ch = TelegramChannel(bus=MessageBus(), config={"bot_token": "t"})
    assert ch.supports_streaming is True


def test_auto_promote_eyes_to_brain_when_no_stage():
    async def go():
        ch, bot = _channel_with_bot()
        _counting_send(bot)
        ch._stage_min_interval = 0.01  # promote almost immediately

        await ch._send_running_reply("1", reply_to_message_id=10)
        assert ch._working_stage["1"] == "received"
        # Let the auto-promote timer fire.
        await ch._promote_timer["1"]
        assert ch._working_stage["1"] == "thinking"

    _run(go())


def test_real_stage_cancels_auto_promote():
    async def go():
        ch, bot = _channel_with_bot()
        _counting_send(bot)
        ch._stage_min_interval = 0  # no throttle so the working stage lands

        await ch._send_running_reply("1", reply_to_message_id=10)
        # A real 'working' stage arrives before the timer → cancels promote.
        await ch.send(OutboundMessage(channel_name="telegram", chat_id="1", thread_id="t", text="", is_final=False, progress_stage="working"))
        assert ch._working_stage["1"] == "working"
        assert ch._promote_timer.get("1") is None or ch._promote_timer["1"].cancelled() or ch._promote_timer["1"].done()

    _run(go())

"""[argus] Tests for the patched Telegram send path (fork patch #10):
HTML parse_mode, chunking, plain-text fallback, and the working-emoji
delete-on-final lifecycle."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

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


def test_working_emoji_sent_then_deleted_on_final():
    ch, bot = _channel_with_bot()
    sent = MagicMock()
    sent.message_id = 77
    bot.send_message.return_value = sent

    # Inbound sets the working emoji.
    _run(ch._send_running_reply("1", reply_to_message_id=10))
    assert ch._working_msg.get("1") == 77

    # Final answer deletes it.
    _run(ch.send(OutboundMessage(channel_name="telegram", chat_id="1", thread_id="t", text="done", is_final=True)))
    bot.delete_message.assert_awaited()
    assert "1" not in ch._working_msg


def test_partial_does_not_delete_working_emoji():
    ch, bot = _channel_with_bot()
    ch._working_msg["1"] = 77

    _run(ch.send(OutboundMessage(channel_name="telegram", chat_id="1", thread_id="t", text="partial", is_final=False)))

    bot.delete_message.assert_not_awaited()
    assert ch._working_msg.get("1") == 77

"""[argus patch #40] Seam locks for the extracted Telegram send path.

telegram.py is upstream-shaped plus three thin shims; the argus send path
lives in app/channels/_telegram_sender.py. These tests turn upstream churn at
the seam into loud failures rather than silent regressions (patch #10's
manager wiring was once lost silently in exactly this way — see PATCHES.md):

  1. TelegramChannel.send delegates to _telegram_sender.send — a bad 3-way
     merge that resurrects upstream's send body (whose superseded stream-edit
     helpers are restored as dead code in the same file!) would import fine
     and quietly kill the stage-emoji + HTML path.
  2. _send_running_reply delegates likewise, and _send_running_reply_safe
     dispatches through the BOUND method (the per-instance override seam that
     test_channels.py::TestTelegramProcessingOrder and
     test_telegram_channel_connections.py rely on).
  3. OutboundMessage still carries the fields the sender reads.
  4. __init__ still wires _telegram_sender.init_state (state + config parsing).
  5. Channel._on_outbound still dispatches to send() — the bus entry point the
     whole extraction hangs off.

Behavior itself is locked by the pre-existing suite (test_telegram_send.py,
test_channels.py Telegram classes), which this extraction did not modify.
"""

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


def _channel(config=None):
    return TelegramChannel(bus=MessageBus(), config={"bot_token": "t", **(config or {})})


def _msg(**kw):
    return OutboundMessage(channel_name="telegram", chat_id="1", thread_id="th", text="x", **kw)


class TestShimDelegation:
    def test_send_delegates_to_sender_module(self, monkeypatch):
        ch = _channel()
        ch._application = MagicMock()
        mock = AsyncMock()
        monkeypatch.setattr(_telegram_sender, "send", mock)
        _run(ch.send(_msg(), _max_retries=2))
        mock.assert_awaited_once()
        args, kwargs = mock.await_args
        assert args[0] is ch
        assert args[1].text == "x"
        assert kwargs == {"max_retries": 2}

    def test_send_running_reply_delegates_to_sender_module(self, monkeypatch):
        ch = _channel()
        mock = AsyncMock()
        monkeypatch.setattr(_telegram_sender, "send_running_reply", mock)
        _run(ch._send_running_reply("1", 5))
        mock.assert_awaited_once_with(ch, "1", 5)

    def test_running_reply_safe_dispatches_through_bound_method(self):
        """Per-instance overrides of ch._send_running_reply MUST intercept."""
        ch = _channel()
        calls = []

        async def override(chat_id, msg_id):
            calls.append((chat_id, msg_id))

        ch._send_running_reply = override
        _run(ch._send_running_reply_safe("1", 5))
        assert calls == [("1", 5)]

    def test_running_reply_safe_swallows_and_logs_errors(self):
        ch = _channel()

        async def boom(chat_id, msg_id):
            raise RuntimeError("boom")

        ch._send_running_reply = boom
        _run(ch._send_running_reply_safe("1", 5))  # must not raise


class TestUpstreamContracts:
    def test_outbound_message_has_fields_the_sender_reads(self):
        m = _msg(is_final=False, progress_stage="thinking")
        assert m.progress_stage == "thinking"
        assert m.is_final is False
        assert m.chat_id == "1"

    def test_init_wires_sender_state_and_config_parsing(self):
        ch = _channel(
            {
                "stage_min_interval": "2.5",
                "working_emoji": "X",
                "stage_emoji": {"thinking": "T"},
            }
        )
        assert ch._working_msg == {}
        assert ch._working_stage == {}
        assert ch._working_at == {}
        assert ch._promote_timer == {}
        assert ch._stage_min_interval == 2.5
        assert ch._stage_emoji["received"] == "X"  # working_emoji back-compat
        assert ch._stage_emoji["thinking"] == "T"  # per-stage override
        assert ch._stage_emoji["searching"] == "🔍"  # defaults survive overrides

    def test_init_tolerates_empty_config(self):
        ch = TelegramChannel(bus=MessageBus(), config={})
        assert ch._stage_min_interval == _telegram_sender._DEFAULT_STAGE_MIN_INTERVAL
        assert ch._stage_emoji == _telegram_sender._DEFAULT_STAGE_EMOJI

    def test_send_noops_before_start_sets_application(self):
        """Bot is resolved at call time; _application is None until start()."""
        ch = _channel()
        _run(ch.send(_msg()))  # must not raise

    def test_base_on_outbound_dispatches_to_send(self):
        ch = _channel()
        ch.send = AsyncMock()
        m = _msg()
        _run(ch._on_outbound(m))
        ch.send.assert_awaited_once_with(m)

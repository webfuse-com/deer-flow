"""Telegram channel — connects via long-polling (no public IP needed)."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from app.channels._telegram_format import chunk_html, to_telegram_html
from app.channels.base import Channel
from app.channels.message_bus import InboundMessage, InboundMessageType, MessageBus, OutboundMessage, ResolvedAttachment

logger = logging.getLogger(__name__)

# [argus patch #10] Two-stage standalone-emoji "working" indicator. Telegram
# renders a lone emoji message large + animated. Show the first emoji
# immediately; after _DEFAULT_WORKING_EMOJI_DELAY seconds, edit it to the
# second. Overridable via channels.telegram.{working_emoji, working_emoji_2,
# working_emoji_delay} in config.yaml.
_DEFAULT_WORKING_EMOJI = "👀"
_DEFAULT_WORKING_EMOJI_2 = "🧠"
_DEFAULT_WORKING_EMOJI_DELAY = 10.0


class TelegramChannel(Channel):
    """Telegram bot channel using long-polling.

    Configuration keys (in ``config.yaml`` under ``channels.telegram``):
        - ``bot_token``: Telegram Bot API token (from @BotFather).
        - ``allowed_users``: (optional) List of allowed Telegram user IDs. Empty = allow all.
    """

    def __init__(self, bus: MessageBus, config: dict[str, Any]) -> None:
        super().__init__(name="telegram", bus=bus, config=config)
        self._application = None
        self._thread: threading.Thread | None = None
        self._tg_loop: asyncio.AbstractEventLoop | None = None
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._allowed_users: set[int] = set()
        for uid in config.get("allowed_users", []):
            try:
                self._allowed_users.add(int(uid))
            except (ValueError, TypeError):
                pass
        # chat_id -> last sent message_id for threaded replies
        self._last_bot_message: dict[str, int] = {}
        # [argus patch #10] chat_id -> message_id of the standalone "working"
        # emoji we sent, so we can delete it when the final answer lands.
        self._working_msg: dict[str, int] = {}
        # [argus patch #10] chat_id -> the asyncio task that swaps the first
        # emoji to the second after a delay; cancelled when the answer lands.
        self._working_timer: dict[str, asyncio.Task] = {}
        # Two-stage working indicator: show the first emoji immediately, then
        # after working_emoji_delay seconds edit it to the second. Whichever is
        # showing is removed when the answer arrives.
        self._working_emoji: str = str(config.get("working_emoji") or _DEFAULT_WORKING_EMOJI)
        self._working_emoji_2: str = str(config.get("working_emoji_2") or _DEFAULT_WORKING_EMOJI_2)
        try:
            self._working_emoji_delay: float = float(config.get("working_emoji_delay", _DEFAULT_WORKING_EMOJI_DELAY))
        except (ValueError, TypeError):
            self._working_emoji_delay = _DEFAULT_WORKING_EMOJI_DELAY

    async def start(self) -> None:
        if self._running:
            return

        try:
            from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters
        except ImportError:
            logger.error("python-telegram-bot is not installed. Install it with: uv add python-telegram-bot")
            return

        bot_token = self.config.get("bot_token", "")
        if not bot_token:
            logger.error("Telegram channel requires bot_token")
            return

        self._main_loop = asyncio.get_event_loop()
        self._running = True
        self.bus.subscribe_outbound(self._on_outbound)

        # Build the application
        app = ApplicationBuilder().token(bot_token).build()

        # Command handlers
        app.add_handler(CommandHandler("start", self._cmd_start))
        app.add_handler(CommandHandler("new", self._cmd_generic))
        app.add_handler(CommandHandler("status", self._cmd_generic))
        app.add_handler(CommandHandler("models", self._cmd_generic))
        app.add_handler(CommandHandler("memory", self._cmd_generic))
        app.add_handler(CommandHandler("help", self._cmd_generic))

        # General message handler
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_text))

        self._application = app

        # Run polling in a dedicated thread with its own event loop
        self._thread = threading.Thread(target=self._run_polling, daemon=True)
        self._thread.start()
        logger.info("Telegram channel started")

    async def stop(self) -> None:
        self._running = False
        self.bus.unsubscribe_outbound(self._on_outbound)
        if self._tg_loop and self._tg_loop.is_running():
            self._tg_loop.call_soon_threadsafe(self._tg_loop.stop)
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None
        self._application = None
        logger.info("Telegram channel stopped")

    async def send(self, msg: OutboundMessage, *, _max_retries: int = 3) -> None:
        if not self._application:
            return

        try:
            chat_id = int(msg.chat_id)
        except (ValueError, TypeError):
            logger.error("Invalid Telegram chat_id: %s", msg.chat_id)
            return

        # [argus patch #10] Convert the agent's markdown to Telegram-native HTML
        # and split on the 4096-char ceiling without breaking tags. Empty text
        # (e.g. an attachment-only message) sends nothing here; send_file
        # handles the upload separately.
        html = to_telegram_html(msg.text) if msg.text else ""
        chunks = chunk_html(html) if html else []

        bot = self._application.bot
        for chunk in chunks:
            await self._send_one(bot, chat_id, msg.chat_id, chunk, _max_retries=_max_retries)

        # [argus patch #10] Only the final message in a response clears the
        # working-indicator emoji. Streaming partials (is_final=False) leave it.
        if msg.is_final:
            await self._clear_working(chat_id, msg.chat_id)

    async def _send_one(self, bot, chat_id: int, chat_key: str, text: str, *, _max_retries: int = 3) -> None:
        """Send a single (already chunked) HTML message with retry + a
        plain-text fallback if Telegram rejects the HTML (a malformed entity
        must never drop the message)."""
        kwargs: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        reply_to = self._last_bot_message.get(chat_key)
        if reply_to:
            kwargs["reply_to_message_id"] = reply_to

        last_exc: Exception | None = None
        for attempt in range(_max_retries):
            try:
                sent = await bot.send_message(**kwargs)
                self._last_bot_message[chat_key] = sent.message_id
                return
            except Exception as exc:
                last_exc = exc
                # A BadRequest is almost always malformed HTML — retrying the
                # same HTML won't help, so drop parse_mode and resend as plain
                # text on the next attempt (mirrors the ateam fallback).
                if exc.__class__.__name__ == "BadRequest" and kwargs.get("parse_mode"):
                    logger.warning("[Telegram] HTML rejected (%s); resending as plain text", exc)
                    kwargs.pop("parse_mode", None)
                    continue
                if attempt < _max_retries - 1:
                    delay = 2**attempt  # 1s, 2s
                    logger.warning(
                        "[Telegram] send failed (attempt %d/%d), retrying in %ds: %s",
                        attempt + 1,
                        _max_retries,
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)

        logger.error("[Telegram] send failed after %d attempts: %s", _max_retries, last_exc)
        if last_exc is None:
            # Degenerate config (_max_retries=0): the loop never ran, so there's
            # no exception to propagate. Surface it explicitly rather than
            # silently succeeding. (Preserves the pre-patch contract.)
            raise RuntimeError("Telegram send failed without an exception from any attempt")
        raise last_exc

    async def send_file(self, msg: OutboundMessage, attachment: ResolvedAttachment) -> bool:
        if not self._application:
            return False

        try:
            chat_id = int(msg.chat_id)
        except (ValueError, TypeError):
            logger.error("[Telegram] Invalid chat_id: %s", msg.chat_id)
            return False

        # Telegram limits: 10MB for photos, 50MB for documents
        if attachment.size > 50 * 1024 * 1024:
            logger.warning("[Telegram] file too large (%d bytes), skipping: %s", attachment.size, attachment.filename)
            return False

        bot = self._application.bot
        reply_to = self._last_bot_message.get(msg.chat_id)

        try:
            if attachment.is_image and attachment.size <= 10 * 1024 * 1024:
                with open(attachment.actual_path, "rb") as f:
                    kwargs: dict[str, Any] = {"chat_id": chat_id, "photo": f}
                    if reply_to:
                        kwargs["reply_to_message_id"] = reply_to
                    sent = await bot.send_photo(**kwargs)
            else:
                from telegram import InputFile

                with open(attachment.actual_path, "rb") as f:
                    input_file = InputFile(f, filename=attachment.filename)
                    kwargs = {"chat_id": chat_id, "document": input_file}
                    if reply_to:
                        kwargs["reply_to_message_id"] = reply_to
                    sent = await bot.send_document(**kwargs)

            self._last_bot_message[msg.chat_id] = sent.message_id
            logger.info("[Telegram] file sent: %s to chat=%s", attachment.filename, msg.chat_id)
            return True
        except Exception:
            logger.exception("[Telegram] failed to send file: %s", attachment.filename)
            return False

    # -- helpers -----------------------------------------------------------

    async def _send_running_reply(self, chat_id: str, reply_to_message_id: int) -> None:
        """[argus patch #10] Two-stage 'working' indicator. Send a standalone
        single-emoji message (Telegram renders these large + animated), store
        its message_id so the final answer can delete it, and schedule a timer
        that edits it to the second emoji after working_emoji_delay seconds.
        If sending the message fails, fall back to a reaction on the user's
        message so there's still a signal.
        """
        if not self._application:
            return
        # A previous indicator for this chat should never linger (e.g. two
        # messages in quick succession): cancel its timer first.
        self._cancel_working_timer(chat_id)

        bot = self._application.bot
        emoji = self._working_emoji
        try:
            sent = await bot.send_message(chat_id=int(chat_id), text=emoji)
            self._working_msg[chat_id] = sent.message_id
            logger.info("[Telegram] working emoji %s sent in chat=%s", emoji, chat_id)
            # Schedule the swap to the second emoji. Runs on the main loop;
            # cancelled by _clear_working when the answer lands.
            self._working_timer[chat_id] = asyncio.ensure_future(
                self._swap_working_emoji(int(chat_id), chat_id, sent.message_id)
            )
            return
        except Exception:
            logger.exception("[Telegram] failed to send working emoji in chat=%s; trying reaction", chat_id)
        # Degraded mode: react on the user's message instead (no timer — the
        # reaction stays until the platform clears it).
        try:
            from telegram import ReactionTypeEmoji

            await bot.set_message_reaction(
                chat_id=int(chat_id),
                message_id=reply_to_message_id,
                reaction=[ReactionTypeEmoji(emoji)],
            )
        except Exception:
            logger.warning("[Telegram] reaction fallback also failed in chat=%s", chat_id)

    async def _swap_working_emoji(self, chat_id: int, chat_key: str, message_id: int) -> None:
        """After a delay, edit the working-emoji message to the second emoji.
        Editing keeps it one message (no delete/resend flicker) and Telegram
        re-animates the new lone emoji. Cancelled if the answer arrives first.
        """
        try:
            await asyncio.sleep(self._working_emoji_delay)
        except asyncio.CancelledError:
            return
        # If the answer already cleared the indicator, do nothing.
        if self._working_msg.get(chat_key) != message_id:
            return
        try:
            await self._application.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=self._working_emoji_2
            )
            logger.info("[Telegram] working emoji → %s in chat=%s", self._working_emoji_2, chat_key)
        except Exception as exc:
            logger.debug("[Telegram] could not swap working emoji in chat=%s: %s", chat_key, exc)

    def _cancel_working_timer(self, chat_key: str) -> None:
        task = self._working_timer.pop(chat_key, None)
        if task and not task.done():
            task.cancel()

    async def _clear_working(self, chat_id: int, chat_key: str) -> None:
        """[argus patch #10] Cancel the pending emoji-swap timer and delete the
        standalone working-emoji message (whichever emoji is showing) once the
        final answer has been sent. Best-effort: the message may already be
        gone (user deleted it, races), so a failure is non-fatal."""
        self._cancel_working_timer(chat_key)
        msg_id = self._working_msg.pop(chat_key, None)
        if msg_id is None:
            return
        try:
            await self._application.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception as exc:
            logger.debug("[Telegram] could not delete working emoji in chat=%s: %s", chat_key, exc)

    # -- internal ----------------------------------------------------------
    @staticmethod
    def _log_future_error(fut, name: str, msg_id: str):
        try:
            exc = fut.exception()
            if exc:
                logger.error("[Telegram] %s failed for msg_id=%s: %s", name, msg_id, exc)
        except Exception:
            logger.exception("[Telegram] Failed to inspect future for %s (msg_id=%s)", name, msg_id)

    def _run_polling(self) -> None:
        """Run telegram polling in a dedicated thread."""
        self._tg_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._tg_loop)
        try:
            # Cannot use run_polling() because it calls add_signal_handler(),
            # which only works in the main thread.  Instead, manually
            # initialize the application and start the updater.
            self._tg_loop.run_until_complete(self._application.initialize())
            self._tg_loop.run_until_complete(self._application.start())
            self._tg_loop.run_until_complete(self._application.updater.start_polling())
            self._tg_loop.run_forever()
        except Exception:
            if self._running:
                logger.exception("Telegram polling error")
        finally:
            # Graceful shutdown
            try:
                if self._application.updater.running:
                    self._tg_loop.run_until_complete(self._application.updater.stop())
                self._tg_loop.run_until_complete(self._application.stop())
                self._tg_loop.run_until_complete(self._application.shutdown())
            except Exception:
                logger.exception("Error during Telegram shutdown")

    def _check_user(self, user_id: int) -> bool:
        if not self._allowed_users:
            return True
        return user_id in self._allowed_users

    async def _cmd_start(self, update, context) -> None:
        """Handle /start command."""
        if not self._check_user(update.effective_user.id):
            return
        await update.message.reply_text("Welcome to DeerFlow! Send me a message to start a conversation.\nType /help for available commands.")

    async def _process_incoming_with_reply(self, chat_id: str, msg_id: int, inbound: InboundMessage) -> None:
        await self._send_running_reply(chat_id, msg_id)
        await self.bus.publish_inbound(inbound)

    async def _cmd_generic(self, update, context) -> None:
        """Forward slash commands to the channel manager."""
        if not self._check_user(update.effective_user.id):
            return

        text = update.message.text
        chat_id = str(update.effective_chat.id)
        user_id = str(update.effective_user.id)
        msg_id = str(update.message.message_id)

        # Use the same topic_id logic as _on_text so that commands
        # like /new target the correct thread mapping.
        if update.effective_chat.type == "private":
            topic_id = None
        else:
            reply_to = update.message.reply_to_message
            if reply_to:
                topic_id = str(reply_to.message_id)
            else:
                topic_id = msg_id

        inbound = self._make_inbound(
            chat_id=chat_id,
            user_id=user_id,
            text=text,
            msg_type=InboundMessageType.COMMAND,
            thread_ts=msg_id,
        )
        inbound.topic_id = topic_id

        if self._main_loop and self._main_loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(self._process_incoming_with_reply(chat_id, update.message.message_id, inbound), self._main_loop)
            fut.add_done_callback(lambda f: self._log_future_error(f, "process_incoming_with_reply", update.message.message_id))
        else:
            logger.warning("[Telegram] Main loop not running. Cannot publish inbound message.")

    async def _on_text(self, update, context) -> None:
        """Handle regular text messages."""
        if not self._check_user(update.effective_user.id):
            return

        text = update.message.text.strip()
        if not text:
            return

        chat_id = str(update.effective_chat.id)
        user_id = str(update.effective_user.id)
        msg_id = str(update.message.message_id)

        # topic_id determines which DeerFlow thread the message maps to.
        # In private chats, use None so that all messages share a single
        # thread (the store key becomes "channel:chat_id").
        # In group chats, use the reply-to message id or the current
        # message id to keep separate conversation threads.
        if update.effective_chat.type == "private":
            topic_id = None
        else:
            reply_to = update.message.reply_to_message
            if reply_to:
                topic_id = str(reply_to.message_id)
            else:
                topic_id = msg_id

        inbound = self._make_inbound(
            chat_id=chat_id,
            user_id=user_id,
            text=text,
            msg_type=InboundMessageType.CHAT,
            thread_ts=msg_id,
        )
        inbound.topic_id = topic_id

        if self._main_loop and self._main_loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(self._process_incoming_with_reply(chat_id, update.message.message_id, inbound), self._main_loop)
            fut.add_done_callback(lambda f: self._log_future_error(f, "process_incoming_with_reply", update.message.message_id))
        else:
            logger.warning("[Telegram] Main loop not running. Cannot publish inbound message.")

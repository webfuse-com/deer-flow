"""Telegram channel — connects via long-polling or webhook.

In polling mode (default), the channel runs ``getUpdates`` long-polling in a
dedicated thread (no public IP needed).

In webhook mode (``webhook: true`` in config), the channel registers a
``POST /webhooks/telegram`` route on the gateway's FastAPI app. Telegram
pushes updates to this endpoint instantly, eliminating the 0-10s polling
delay. Requires a public HTTPS URL (set via ``setWebhook``); the route
verifies the ``X-Telegram-Bot-Api-Secret-Token`` header.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

from fastapi import Request, Response

from app.channels import _telegram_sender
from app.channels.base import Channel
from app.channels.connection_identity import attach_connection_identity
from app.channels.message_bus import InboundMessage, InboundMessageType, MessageBus, OutboundMessage, ResolvedAttachment

logger = logging.getLogger(__name__)

TELEGRAM_MAX_MESSAGE_LENGTH = 4096
STREAM_EDIT_MIN_INTERVAL_SECONDS = 1.0
# Groups (negative chat_id) are capped at 20 messages/minute by Telegram,
# so stream edits there must pace well below the private-chat 1 msg/s guideline.
STREAM_EDIT_GROUP_MIN_INTERVAL_SECONDS = 3.0
# Bound on tracked in-flight streamed messages; entries normally clear on the
# final update, this only guards against leaks when a final never arrives.
MAX_TRACKED_STREAM_MESSAGES = 256

# Indirection so tests can patch the clock without touching the global time module.
_monotonic = time.monotonic

# [argus patch #40] The constants above and the class's stream-edit helpers
# (_send_stream_update through _split_message, and the _stream_messages init)
# are upstream v2.0.0's edit-in-place streaming path, restored byte-identical
# but UNREACHABLE: send() delegates to app.channels._telegram_sender (the
# stage-emoji + HTML design, patch #9-chain). Kept verbatim so upstream churn
# in these regions merges clean instead of modify/delete-conflicting on every
# sync. Do not call them; do not "clean them up". See PATCHES.md #40.


class TelegramChannel(Channel):
    """Telegram bot channel using long-polling or webhook.

    Configuration keys (in ``config.yaml`` under ``channels.telegram``):
        - ``bot_token``: Telegram Bot API token (from @BotFather).
        - ``allowed_users``: (optional) List of allowed Telegram user IDs. Empty = allow all.
        - ``webhook``: (optional) When true, use webhook mode instead of polling.
        - ``webhook_secret``: (optional) Secret token for webhook verification.
          When set, the ``/webhooks/telegram`` route rejects requests whose
          ``X-Telegram-Bot-Api-Secret-Token`` header doesn't match.
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
        # stream_key ("chat_id:thread_ts") -> state of the in-flight streamed
        # bot message being edited in place: {"message_id", "last_edit_at", "last_text"}
        self._stream_messages: dict[str, dict[str, Any]] = {}
        # [argus patch #40] Stage-emoji indicator state + config parsing
        # (stage_emoji / working_emoji / stage_min_interval) — see _telegram_sender.
        _telegram_sender.init_state(self, config)

        # Webhook mode config.
        self._webhook_mode: bool = bool(config.get("webhook", False))
        self._webhook_secret: str = str(config.get("webhook_secret", ""))

    # [argus patch #10] Take the manager's streaming path so we receive
    # progress_stage signals to drive the animated stage emoji. The manager's
    # _channel_supports_streaming reads THIS property (not CHANNEL_CAPABILITIES)
    # whenever a live channel object exists, so the capability flip alone is not
    # enough — this override is what actually engages streaming. (Telegram still
    # gets only stage signals + the final answer, never streamed partial text;
    # that suppression lives in the manager.)
    @property
    def supports_streaming(self) -> bool:
        return True

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
        # /bootstrap routes into a bootstrap session in the manager (is_bootstrap),
        # same as the other channels — register it so Telegram users can use it.
        app.add_handler(CommandHandler("bootstrap", self._cmd_generic))
        app.add_handler(CommandHandler("new", self._cmd_generic))
        app.add_handler(CommandHandler("status", self._cmd_generic))
        app.add_handler(CommandHandler("models", self._cmd_generic))
        app.add_handler(CommandHandler("memory", self._cmd_generic))
        app.add_handler(CommandHandler("help", self._cmd_generic))

        # General message handler. Plain text -> _on_text; an UNKNOWN slash
        # command (TEXT & COMMAND not matched above) also routes through
        # _on_text so it reaches the agent as chat rather than being dropped
        # (upstream v2.0.0 behavior).
        app.add_handler(MessageHandler(filters.TEXT & filters.COMMAND, self._on_text))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_text))

        self._application = app

        if self._webhook_mode:
            # Webhook mode: register a FastAPI route on the gateway app.
            # No polling thread, no separate event loop — Telegram pushes
            # updates to us and the gateway's event loop handles them.
            await self._register_webhook_route()
            logger.info("Telegram channel started (webhook mode)")
        else:
            # Polling mode: run getUpdates in a dedicated thread.
            self._thread = threading.Thread(target=self._run_polling, daemon=True)
            self._thread.start()
            logger.info("Telegram channel started (polling mode)")

    async def stop(self) -> None:
        self._running = False
        self.bus.unsubscribe_outbound(self._on_outbound)
        if not self._webhook_mode:
            # Polling mode: stop the dedicated thread + event loop.
            if self._tg_loop and self._tg_loop.is_running():
                self._tg_loop.call_soon_threadsafe(self._tg_loop.stop)
            if self._thread:
                self._thread.join(timeout=10)
                self._thread = None
        elif self._application:
            # Webhook mode: just stop the application (no thread to join).
            try:
                await self._application.stop()
                await self._application.shutdown()
            except Exception:
                logger.exception("Error during Telegram webhook shutdown")
        self._application = None
        logger.info("Telegram channel stopped")

    async def send(self, msg: OutboundMessage, *, _max_retries: int = 3) -> None:
        # [argus patch #40] The argus send path (stage emoji + HTML chunking,
        # patch #9-chain) lives in app.channels._telegram_sender; this shim is
        # the Channel ABC entry point. Signature — including the test-facing
        # _max_retries kwarg — matches upstream v2.0.0. The stream-edit helpers
        # below are upstream's superseded path, kept verbatim but unreachable.
        await _telegram_sender.send(self, msg, max_retries=_max_retries)

    async def _send_stream_update(self, chat_id: int, key: str, text: str, reply_to: int | None = None) -> None:
        """Edit the in-flight streamed message with accumulated text.

        Updates are best-effort: throttled, rate-limit drops are silent.  The
        manager always publishes a final message afterwards, which guarantees
        delivery of the complete text.
        """
        if not text:
            return

        display = text
        if len(display) > TELEGRAM_MAX_MESSAGE_LENGTH:
            display = display[: TELEGRAM_MAX_MESSAGE_LENGTH - 1] + "…"

        bot = self._application.bot
        state = self._stream_messages.get(key)

        send_kwargs: dict[str, Any] = {"chat_id": chat_id, "text": display}
        if reply_to:
            send_kwargs["reply_to_message_id"] = reply_to

        if state is None:
            try:
                sent = await bot.send_message(**send_kwargs)
            except Exception:
                logger.exception("[Telegram] failed to start stream message in chat=%s", chat_id)
                return
            self._register_stream_message(key, message_id=sent.message_id, last_text=display, last_edit_at=_monotonic())
            return

        now = _monotonic()
        min_interval = STREAM_EDIT_GROUP_MIN_INTERVAL_SECONDS if chat_id < 0 else STREAM_EDIT_MIN_INTERVAL_SECONDS
        if now - state["last_edit_at"] < min_interval:
            return
        if display == state["last_text"]:
            return

        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=state["message_id"], text=display)
        except Exception as exc:
            if self._is_not_modified(exc):
                state["last_text"] = display
                return
            if self._is_retry_after(exc):
                logger.debug("[Telegram] stream edit rate-limited in chat=%s, dropping update", chat_id)
                return
            logger.warning("[Telegram] stream edit failed in chat=%s, sending new message: %s", chat_id, exc)
            try:
                sent = await bot.send_message(**send_kwargs)
            except Exception:
                logger.exception("[Telegram] failed to send fallback stream message in chat=%s", chat_id)
                return
            state["message_id"] = sent.message_id

        state["last_edit_at"] = _monotonic()
        state["last_text"] = display

    async def _finalize_stream_message(self, chat_id: int, chat_key: str, state: dict[str, Any], text: str) -> None:
        """Apply the final text: edit the streamed message, splitting overflow into follow-ups."""
        bot = self._application.bot
        chunks = self._split_message(text or "")

        edited = True
        if chunks[0] != state["last_text"]:
            edited = await self._edit_final_chunk(bot, chat_id, state["message_id"], chunks[0])

        if edited:
            self._last_bot_message[chat_key] = state["message_id"]
        else:
            # Edit could not be applied (e.g. message deleted) — deliver the
            # first chunk as a fresh message with the standard retry policy.
            await self._send_new_message(chat_id, chat_key, chunks[0])

        for chunk in chunks[1:]:
            await self._send_new_message(chat_id, chat_key, chunk)

    async def _edit_final_chunk(self, bot, chat_id: int, message_id: int, text: str) -> bool:
        """Edit with one rate-limit retry. Returns False if the edit could not be applied."""
        for attempt in range(2):
            try:
                await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
                return True
            except Exception as exc:
                if self._is_not_modified(exc):
                    return True
                if self._is_retry_after(exc) and attempt == 0:
                    await asyncio.sleep(self._retry_after_seconds(exc))
                    continue
                logger.warning("[Telegram] final edit failed in chat=%s: %s", chat_id, exc)
                return False
        return False

    async def _send_new_message(self, chat_id: int, chat_key: str, text: str, *, _max_retries: int = 3) -> int | None:
        """Send a fresh message with retry/backoff. Returns the sent message_id."""
        kwargs: dict[str, Any] = {"chat_id": chat_id, "text": text}

        # Reply to the last bot message in this chat for threading
        reply_to = self._last_bot_message.get(chat_key)
        if reply_to:
            kwargs["reply_to_message_id"] = reply_to

        bot = self._application.bot

        async def send_message() -> int:
            sent = await bot.send_message(**kwargs)
            self._last_bot_message[chat_key] = sent.message_id
            return sent.message_id

        return await self._send_with_retry(
            send_message,
            max_retries=_max_retries,
            log_prefix="[Telegram]",
        )

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

    @staticmethod
    def _stream_key(chat_id: str, thread_ts: str | None) -> str:
        return f"{chat_id}:{thread_ts or ''}"

    @staticmethod
    def _parse_message_id(value: str | None) -> int | None:
        try:
            return int(value) if value else None
        except (TypeError, ValueError):
            return None

    def _register_stream_message(self, key: str, *, message_id: int, last_text: str, last_edit_at: float) -> None:
        self._stream_messages.pop(key, None)
        while len(self._stream_messages) >= MAX_TRACKED_STREAM_MESSAGES:
            self._stream_messages.pop(next(iter(self._stream_messages)))
        self._stream_messages[key] = {
            "message_id": message_id,
            "last_edit_at": last_edit_at,
            "last_text": last_text,
        }

    @staticmethod
    def _is_retry_after(exc: Exception) -> bool:
        return getattr(exc, "retry_after", None) is not None

    @staticmethod
    def _retry_after_seconds(exc: Exception) -> float:
        value = getattr(exc, "retry_after", 0)
        if hasattr(value, "total_seconds"):
            return float(value.total_seconds())
        return float(value)

    @staticmethod
    def _is_not_modified(exc: Exception) -> bool:
        return "message is not modified" in str(exc).lower()

    @staticmethod
    def _split_message(text: str) -> list[str]:
        return [text[i : i + TELEGRAM_MAX_MESSAGE_LENGTH] for i in range(0, len(text), TELEGRAM_MAX_MESSAGE_LENGTH)] or [text]

    async def _send_running_reply(self, chat_id: str, reply_to_message_id: int) -> None:
        # [argus patch #40] Body in _telegram_sender (initial 'received' stage
        # emoji + reaction fallback). Kept as a bound method: tests replace it
        # per instance, and _send_running_reply_safe dispatches through `self`
        # so those overrides keep working.
        await _telegram_sender.send_running_reply(self, chat_id, reply_to_message_id)

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

    # -- webhook mode ------------------------------------------------------

    async def _register_webhook_route(self) -> None:
        """Register the /webhooks/telegram POST route on the gateway FastAPI app.

        In webhook mode, Telegram pushes updates to this endpoint. The route
        deserializes the Update JSON and feeds it into the same handler
        functions used by polling mode. This runs entirely on the gateway's
        event loop — no separate thread or loop needed.
        """
        # Initialize the application (bot + handlers) without starting polling.
        await self._application.initialize()
        await self._application.start()

        # Find the gateway's FastAPI app instance.
        # The gateway creates it as a module-level `app` in app.gateway.app.
        from app.gateway.app import app as gateway_app

        telegram_channel = self

        @gateway_app.post("/webhooks/telegram")
        async def telegram_webhook(request: Request) -> Response:
            # Verify the secret token if configured.
            if telegram_channel._webhook_secret:
                secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
                if secret != telegram_channel._webhook_secret:
                    logger.warning("[Telegram] webhook rejected: invalid secret token")
                    return Response(status_code=403)

            # Deserialize the update and feed it to the handlers.
            try:
                from telegram import Update

                body = await request.json()
                update = Update.de_json(body, telegram_channel._application.bot)

                # Dispatch to the registered handlers. In webhook mode we call
                # process_update directly on the main loop — no thread hop needed.
                # The registered handlers (_cmd_start / _on_text / _cmd_generic)
                # attach connection identity before publishing, so the webhook
                # path inherits user-owned connection binding for free.
                await telegram_channel._application.process_update(update)
            except Exception:
                logger.exception("[Telegram] webhook handler error")
                return Response(status_code=500)

            return Response(status_code=200)

        logger.info("[Telegram] webhook route registered at POST /webhooks/telegram")

    def _check_user(self, user_id: int) -> bool:
        # [argus patch #17] Trust-on-first-use lock. A stack whose config ships
        # an empty allowed_users (e.g. the Console paste-token wizard before it
        # captured a numeric id) would otherwise reply to ANY Telegram user who
        # finds the bot. Instead: bind to the FIRST sender's id and reject all
        # others. This is defense in depth — the Console should still write the
        # citizen's id into allowed_users so the bot is locked from minute one.
        if not self._allowed_users:
            self._allowed_users.add(int(user_id))
            logger.warning(
                "[Telegram] allowed_users was empty; trust-on-first-use bound this "
                "bot to user_id=%s. Set channels.telegram.allowed_users to lock it "
                "explicitly.", user_id,
            )
            return True
        return int(user_id) in self._allowed_users

    @staticmethod
    def _telegram_display_name(user) -> str:
        full_name = getattr(user, "full_name", None)
        if isinstance(full_name, str) and full_name:
            return full_name
        username = getattr(user, "username", None)
        if isinstance(username, str) and username:
            return username
        return str(getattr(user, "id", ""))

    async def _bind_connection_from_start_token(self, update, state_token: str) -> bool:
        if self._connection_repo is None or not state_token:
            return False

        state = await self._connection_repo.consume_oauth_state(provider="telegram", state=state_token)
        if state is None:
            await update.message.reply_text("Telegram connection link is invalid or expired.")
            return True

        owner_user_id = state["owner_user_id"]
        user_id = str(update.effective_user.id)
        chat_id = str(update.effective_chat.id)
        connection = await self._connection_repo.upsert_connection(
            owner_user_id=owner_user_id,
            provider="telegram",
            external_account_id=user_id,
            external_account_name=self._telegram_display_name(update.effective_user),
            workspace_id=chat_id,
            workspace_name=None,
            metadata={
                "chat_id": chat_id,
                "chat_type": update.effective_chat.type,
                "telegram_username": getattr(update.effective_user, "username", None),
            },
            status="connected",
        )
        logger.info("[Telegram] bound chat=%s user=%s to DeerFlow user=%s connection=%s", chat_id, user_id, owner_user_id, connection["id"])
        await update.message.reply_text("Telegram connected to DeerFlow.")
        return True

    async def _attach_connection_identity(self, inbound: InboundMessage) -> InboundMessage:
        return await attach_connection_identity(
            inbound,
            repo=self._connection_repo,
            provider="telegram",
            workspace_id=inbound.chat_id,
        )

    def _get_bot_username(self, context) -> str | None:
        bot = getattr(context, "bot", None)
        username = getattr(bot, "username", None)
        if not username and self._application is not None:
            username = getattr(getattr(self._application, "bot", None), "username", None)
        return str(username) if username else None

    @staticmethod
    def _strip_bot_username_from_leading_command(text: str, bot_username: str | None) -> str:
        username = (bot_username or "").lstrip("@").lower()
        if not username or not text.startswith("/"):
            return text

        parts = text.split(maxsplit=1)
        command_token = parts[0]
        if "@" not in command_token:
            return text

        command_name, addressed_username = command_token[1:].rsplit("@", 1)
        if not command_name or addressed_username.lower() != username:
            return text

        normalized = f"/{command_name}"
        if len(parts) > 1:
            normalized = f"{normalized} {parts[1]}"
        return normalized

    async def _cmd_start(self, update, context) -> None:
        """Handle /start command."""
        # Handle the deep-link bind token before applying allowed_users / the
        # welcome reply, so a browser-initiated bind can bootstrap a new
        # external identity the bot has never seen and is therefore not yet
        # authorized for.
        args = getattr(context, "args", []) if context is not None else []
        if args:
            handled = await self._bind_connection_from_start_token(update, str(args[0]))
            if handled:
                return
        if not self._check_user(update.effective_user.id):
            return
        await update.message.reply_text(self._welcome_text(), parse_mode="HTML")

    def _welcome_text(self) -> str:
        """[argus patch #17] Role/persona-aware Telegram greeting.

        The greeting comes from ``channels.telegram.welcome`` in config.yaml,
        which the atlas-template fills per citizen (name + role + capabilities)
        at expand time. Falls back to an Atlas-voiced default if unset — never
        the bare "Welcome to DeerFlow!" upstream string.
        """
        configured = self.config.get("welcome")
        if configured:
            return str(configured).strip()
        name = str(self.config.get("citizen_name", "") or "").strip()
        hi = f"Hi {name}" if name else "Hi"
        return (
            f"{hi}, I'm <b>Atlas</b>, your personal agent. I can review your inbox, "
            "prep you for meetings, track your tasks and tickets, and answer "
            "questions from the company knowledge base.\n\n"
            "Just tell me what you need. /help lists commands."
        )

    async def _process_incoming_with_reply(self, chat_id: str, msg_id: int, inbound: InboundMessage) -> None:
        # Fire the 👀 received emoji in the background so the inbound message
        # reaches the agent pipeline immediately. The emoji is a UI indicator,
        # not functional — if it fails, the degraded mode (reaction fallback)
        # already handles it. Saves ~200ms of Telegram API round-trip on the
        # critical path.
        asyncio.create_task(self._send_running_reply_safe(chat_id, msg_id))
        await self.bus.publish_inbound(inbound)

    async def _send_running_reply_safe(self, chat_id: str, msg_id: int) -> None:
        """Fire-and-forget wrapper for _send_running_reply that logs errors."""
        # [argus patch #40] Body in _telegram_sender (dispatches via the bound
        # self._send_running_reply, preserving per-instance overrides).
        await _telegram_sender.send_running_reply_safe(self, chat_id, msg_id)

    async def _cmd_generic(self, update, context) -> None:
        """Forward slash commands to the channel manager."""
        if not self._check_user(update.effective_user.id):
            return

        text = self._strip_bot_username_from_leading_command(update.message.text.strip(), self._get_bot_username(context))
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
            metadata={"message_id": msg_id},
        )
        inbound.topic_id = topic_id
        inbound = await self._attach_connection_identity(inbound)

        if self._main_loop and self._main_loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(self._process_incoming_with_reply(chat_id, update.message.message_id, inbound), self._main_loop)
            fut.add_done_callback(lambda f: self._log_future_error(f, "process_incoming_with_reply", update.message.message_id))
        else:
            logger.warning("[Telegram] Main loop not running. Cannot publish inbound message.")

    async def _on_text(self, update, context) -> None:
        """Handle regular text messages."""
        if not self._check_user(update.effective_user.id):
            return

        text = self._strip_bot_username_from_leading_command(update.message.text.strip(), self._get_bot_username(context))
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
            metadata={"message_id": msg_id},
        )
        inbound.topic_id = topic_id
        inbound = await self._attach_connection_identity(inbound)

        if self._main_loop and self._main_loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(self._process_incoming_with_reply(chat_id, update.message.message_id, inbound), self._main_loop)
            fut.add_done_callback(lambda f: self._log_future_error(f, "process_incoming_with_reply", update.message.message_id))
        else:
            logger.warning("[Telegram] Main loop not running. Cannot publish inbound message.")

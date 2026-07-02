"""[argus patch #40] Telegram send path: stage-emoji indicator + HTML chunked sends.

Extracted verbatim from app/channels/telegram.py to shrink the fork's
upstream-edited-line carry (the dominant merge tax). telegram.py keeps thin
bound-method shims (send / _send_running_reply / _send_running_reply_safe)
plus upstream's own code; everything argus-specific about SENDING lives here.

Behavior (ledgered as patch #9-chain):
  * a progress_stage OutboundMessage renders as an animated lone-emoji message,
    deleted + re-sent on stage change (Telegram animates only on first send),
    throttled to stage_min_interval, with a received->thinking auto-promote;
  * chat text converts markdown -> Telegram HTML (_telegram_format) and chunks
    at 4096, with retry + a plain-text fallback when Telegram rejects the HTML;
  * only the final message clears the indicator.

Free functions take the TelegramChannel as first argument; ALL mutable state
lives on the channel instance (see init_state) because tests read/patch
ch._working_msg & friends directly, and per-instance overrides of
ch._send_running_reply must keep working (send_running_reply_safe dispatches
through the bound method, never the module function). The bot is resolved from
channel._application at call time — it is None until the channel's start().

Historical note: inline "[argus patch #10]" markers in the moved bodies are
the pre-ledger label for the #9-chain stage-emoji work and are left verbatim
on purpose; this extraction is patch #40 — see PATCHES.md.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from app.channels._telegram_format import chunk_html, to_telegram_html

if TYPE_CHECKING:
    from app.channels.message_bus import OutboundMessage
    from app.channels.telegram import TelegramChannel

logger = logging.getLogger(__name__)

# [argus patch #10] Live stage-emoji progress indicator. Telegram renders a
# lone-emoji message large + animated, but the animation plays ONCE on send
# and never replays on edit (core.telegram.org/api/animated-emojis). So to
# animate each stage we DELETE the old emoji message and SEND a new one when
# the agent's stage changes. The manager derives stages from the langgraph
# stream; this maps each to an emoji. Re-sends are throttled to >= the
# animation length so each one completes. Overridable via
# channels.telegram.{stage_emoji (map), stage_min_interval, working_emoji}.
_DEFAULT_STAGE_EMOJI = {
    "received": "👀",
    "thinking": "🧠",
    "planning": "📝",
    "searching": "🔍",
    "working": "🔧",
    "writing": "✍️",
}
_DEFAULT_STAGE_MIN_INTERVAL = 6.0  # seconds; matches the ~6s big-emoji animation


def init_state(channel: TelegramChannel, config: dict[str, Any]) -> None:
    """Initialize the sender's per-chat state ON the channel instance and
    parse its config knobs. Called from TelegramChannel.__init__; must not
    touch channel._application (set later, in start())."""
    # [argus patch #10] Live stage-emoji indicator state, per chat:
    #   _working_msg[chat]   -> message_id of the current stage emoji
    #   _working_stage[chat] -> the stage that emoji represents
    #   _working_at[chat]    -> monotonic time we last (re)sent it
    channel._working_msg = {}
    channel._working_stage = {}
    channel._working_at = {}
    # chat -> task that auto-promotes 👀 received → 🧠 thinking after
    # stage_min_interval if no real stage signal has arrived. Guarantees the
    # indicator always advances even when the agent reports no tool stage.
    channel._promote_timer = {}
    # Stage → emoji map (config override merges over the defaults).
    channel._stage_emoji = dict(_DEFAULT_STAGE_EMOJI)
    cfg_map = config.get("stage_emoji")
    if isinstance(cfg_map, dict):
        channel._stage_emoji.update({str(k): str(v) for k, v in cfg_map.items()})
    # Back-compat: a bare working_emoji still overrides the initial beat.
    if config.get("working_emoji"):
        channel._stage_emoji["received"] = str(config["working_emoji"])
    try:
        channel._stage_min_interval = float(config.get("stage_min_interval", _DEFAULT_STAGE_MIN_INTERVAL))
    except (ValueError, TypeError):
        channel._stage_min_interval = _DEFAULT_STAGE_MIN_INTERVAL


async def send(channel: TelegramChannel, msg: OutboundMessage, *, max_retries: int = 3) -> None:
    if not channel._application:
        return

    try:
        chat_id = int(msg.chat_id)
    except (ValueError, TypeError):
        logger.error("Invalid Telegram chat_id: %s", msg.chat_id)
        return

    # [argus patch #10] A progress signal is not chat content — render it as
    # the animated stage emoji and return BEFORE the HTML/chunk send path.
    if msg.progress_stage is not None:
        await show_stage(channel, chat_id, msg.chat_id, msg.progress_stage)
        return

    # [argus patch #10] Convert the agent's markdown to Telegram-native HTML
    # and split on the 4096-char ceiling without breaking tags. Empty text
    # (e.g. an attachment-only message) sends nothing here; send_file
    # handles the upload separately.
    html = to_telegram_html(msg.text) if msg.text else ""
    chunks = chunk_html(html) if html else []

    bot = channel._application.bot
    for chunk in chunks:
        await send_one(channel, bot, chat_id, msg.chat_id, chunk, max_retries=max_retries)

    # [argus patch #10] Only the final message in a response clears the
    # working-indicator emoji. Streaming partials (is_final=False) leave it.
    if msg.is_final:
        await clear_working(channel, chat_id, msg.chat_id)


async def send_one(channel: TelegramChannel, bot, chat_id: int, chat_key: str, text: str, *, max_retries: int = 3) -> None:
    """Send a single (already chunked) HTML message with retry + a
    plain-text fallback if Telegram rejects the HTML (a malformed entity
    must never drop the message)."""
    kwargs: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    reply_to = channel._last_bot_message.get(chat_key)
    if reply_to:
        kwargs["reply_to_message_id"] = reply_to

    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            sent = await bot.send_message(**kwargs)
            channel._last_bot_message[chat_key] = sent.message_id
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
            if attempt < max_retries - 1:
                delay = 2**attempt  # 1s, 2s
                logger.warning(
                    "[Telegram] send failed (attempt %d/%d), retrying in %ds: %s",
                    attempt + 1,
                    max_retries,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)

    logger.error("[Telegram] send failed after %d attempts: %s", max_retries, last_exc)
    if last_exc is None:
        # Degenerate config (_max_retries=0): the loop never ran, so there's
        # no exception to propagate. Surface it explicitly rather than
        # silently succeeding. (Preserves the pre-patch contract.)
        raise RuntimeError("Telegram send failed without an exception from any attempt")
    raise last_exc


async def send_running_reply(channel: TelegramChannel, chat_id: str, reply_to_message_id: int) -> None:
    """[argus patch #10] Show the initial 'received' stage emoji as soon as
    a message arrives. Subsequent stages come from the manager as
    progress_stage OutboundMessages and are handled by show_stage. If the
    emoji send fails, fall back to a reaction on the user's message."""
    if not channel._application:
        return
    ok = await show_stage(channel, int(chat_id), chat_id, "received")
    if ok:
        return
    # Degraded mode: react on the user's message instead.
    try:
        from telegram import ReactionTypeEmoji

        await channel._application.bot.set_message_reaction(
            chat_id=int(chat_id),
            message_id=reply_to_message_id,
            reaction=[ReactionTypeEmoji(channel._stage_emoji.get("received", "👀"))],
        )
    except Exception:
        logger.warning("[Telegram] reaction fallback also failed in chat=%s", chat_id)


async def send_running_reply_safe(channel: TelegramChannel, chat_id: str, msg_id: int) -> None:
    """Fire-and-forget wrapper for _send_running_reply that logs errors.

    Dispatches through the BOUND channel._send_running_reply (never the
    module-level send_running_reply) so per-instance overrides of that
    method — a documented test seam — keep intercepting."""
    try:
        await channel._send_running_reply(chat_id, msg_id)
    except Exception:
        logger.exception("[Telegram] fire-and-forget _send_running_reply failed in chat=%s", chat_id)


async def show_stage(channel: TelegramChannel, chat_id: int, chat_key: str, stage: str, *, force: bool = False) -> bool:
    """[argus patch #10] Render a stage as the animated lone emoji.

    The big-emoji animation only plays on first SEND (an edit never
    replays it), so to animate each new stage we delete the prior emoji
    message and send a fresh one. Re-send only when (a) the stage's emoji
    actually changes and (b) at least stage_min_interval has elapsed since
    the last send — so each animation completes and we stay well under
    Telegram's ~1 msg/sec per-chat limit. Rapid intermediate stages are
    skipped (latest-wins). ``force`` bypasses the interval guard (used by
    the 👀→🧠 auto-promote timer, which already waited the interval).
    Returns True if an emoji is now showing.
    """
    if not channel._application:
        return False
    emoji = channel._stage_emoji.get(stage)
    if not emoji:
        return bool(channel._working_msg.get(chat_key))

    now = time.monotonic()
    cur_stage = channel._working_stage.get(chat_key)
    have_msg = chat_key in channel._working_msg

    # No change, or too soon since the last (re)send → leave it be. The next
    # eligible stage signal will carry the then-current stage (latest-wins).
    if have_msg and cur_stage == stage:
        return True
    if have_msg and not force and (now - channel._working_at.get(chat_key, 0.0)) < channel._stage_min_interval:
        return True

    bot = channel._application.bot
    old_id = channel._working_msg.get(chat_key)
    try:
        sent = await bot.send_message(chat_id=chat_id, text=emoji)
    except Exception:
        logger.exception("[Telegram] failed to send stage emoji in chat=%s", chat_key)
        return have_msg
    channel._working_msg[chat_key] = sent.message_id
    channel._working_stage[chat_key] = stage
    channel._working_at[chat_key] = now
    logger.info("[Telegram] stage %s (%s) in chat=%s", stage, emoji, chat_key)
    # Delete the previous emoji after the new one is up (no visible gap).
    if old_id is not None:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=old_id)
        except Exception as exc:
            logger.debug("[Telegram] could not delete prior stage emoji in chat=%s: %s", chat_key, exc)
    # Any real stage supersedes a pending auto-promote; 'received' (re)arms it.
    cancel_promote(channel, chat_key)
    if stage == "received":
        schedule_promote(channel, chat_id, chat_key)
    return True


def schedule_promote(channel: TelegramChannel, chat_id: int, chat_key: str) -> None:
    """Arm the 👀→🧠 auto-promotion so the indicator always advances even
    if the agent never reports a tool stage."""
    channel._promote_timer[chat_key] = asyncio.ensure_future(auto_promote(channel, chat_id, chat_key))


def cancel_promote(channel: TelegramChannel, chat_key: str) -> None:
    task = channel._promote_timer.pop(chat_key, None)
    if task and not task.done():
        task.cancel()


async def auto_promote(channel: TelegramChannel, chat_id: int, chat_key: str) -> None:
    try:
        await asyncio.sleep(channel._stage_min_interval)
    except asyncio.CancelledError:
        return
    # Drop our own handle first so show_stage's cancel_promote (which runs
    # for every send) doesn't cancel this still-running task mid-flight.
    channel._promote_timer.pop(chat_key, None)
    # Only promote if we're still on 'received' (no real stage arrived).
    if channel._working_stage.get(chat_key) == "received":
        await show_stage(channel, chat_id, chat_key, "thinking", force=True)


async def clear_working(channel: TelegramChannel, chat_id: int, chat_key: str) -> None:
    """[argus patch #10] Delete the current stage emoji once the final
    answer has been sent. Best-effort — the message may already be gone."""
    cancel_promote(channel, chat_key)
    channel._working_stage.pop(chat_key, None)
    channel._working_at.pop(chat_key, None)
    msg_id = channel._working_msg.pop(chat_key, None)
    if msg_id is None:
        return
    try:
        await channel._application.bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except Exception as exc:
        logger.debug("[Telegram] could not delete stage emoji in chat=%s: %s", chat_key, exc)

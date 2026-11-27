"""[argus patch #178] Telegram approval cards for staged actions: the fork's hook.

An Atlas stack's agent may stage an action for its owner to approve (atlas-mcp
``action_stage``). On Telegram the owner gets one card per staged action after
the reply, with Approve and Skip buttons. What a card says, who may press it and
the calls to atlas-mcp all live in the Argus overlay module
``argus_telegram_actions`` (acropolis ``docker/deerflow-backend``), which is
soft-imported here: without it, or when it raises, Telegram behaves as before.

The overlay cannot do this alone. It has no way to learn which actions a turn
staged (the manager holds the turn's tool results), to send a message after the
final reply (the send path owns the bot and the order), or to receive a button
press (handlers are registered on the adapter's application). These three
functions are those seams, one call site each:

* ``stamp_staged_actions``: manager, the final reply's metadata;
* ``send_cards``: ``_telegram_sender.send``, after the final reply;
* ``register``: ``TelegramChannel.start``, the ``callback_query`` handler.

Nothing here logs a payload, a token or an answer's text.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.channels.message_bus import OutboundMessage
    from app.channels.telegram import TelegramChannel

logger = logging.getLogger(__name__)

STAGED_ACTIONS_KEY = "argus_staged_actions"


def stamp_staged_actions(metadata: dict[str, Any], channel_name: str, turn_messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Put the ids of the actions this turn staged on a Telegram reply's metadata.

    Returns ``metadata`` (the same dict) so a call site can wrap the expression
    it already has. A scheduled run's reply takes the same path as a chat turn's.
    """
    if channel_name != "telegram":
        return metadata
    try:
        from argus_telegram_actions import staged_action_ids
    except ImportError:
        return metadata
    try:
        ids = staged_action_ids([m for m in turn_messages if isinstance(m, dict) and m.get("type") == "tool"])
        if ids:
            metadata[STAGED_ACTIONS_KEY] = list(ids)
    except Exception as exc:
        logger.warning("[Telegram] staged-action ids not read: %s", type(exc).__name__)
    return metadata


async def send_cards(channel: TelegramChannel, bot: Any, chat_id: int, msg: OutboundMessage) -> None:
    """Send one approval card per staged action, as messages of their own.

    Plain text and one attempt each: a card shows a payload verbatim, so it is
    never parsed as HTML, and a card that fails to send leaves the action in the
    web Inbox. A failure never reaches the reply that was already sent.
    """
    ids = (msg.metadata or {}).get(STAGED_ACTIONS_KEY)
    if not ids:
        return
    try:
        from argus_telegram_actions import cards
    except ImportError:
        return
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        for card in await cards(ids, channel._allowed_users, chat_id):
            rows = (card.get("reply_markup") or {}).get("inline_keyboard") or []
            markup = InlineKeyboardMarkup([[InlineKeyboardButton(text=b["text"], callback_data=b["callback_data"]) for b in row] for row in rows]) if rows else None
            await bot.send_message(chat_id=chat_id, text=card["text"], reply_markup=markup)
    except Exception as exc:
        logger.warning("[Telegram] staged-action cards not sent: %s", type(exc).__name__)


async def on_press(channel: TelegramChannel, update: Any) -> None:
    """Hand a button press to the overlay, answer the callback query and edit the card.

    The overlay decides whether the press counts (the one allowlisted owner, in
    their private chat) and makes the calls. A press nobody can act on is still
    answered, so the button stops spinning.
    """
    query = getattr(update, "callback_query", None)
    if query is None:
        return
    shown: dict[str, Any] = {}
    try:
        from argus_telegram_actions import on_callback

        shown = await on_callback(update.to_dict(), channel._allowed_users) or {}
    except ImportError:
        logger.warning("[Telegram] staged-action overlay is not installed")
    except Exception as exc:
        logger.warning("[Telegram] staged-action press not handled: %s", type(exc).__name__)
    try:
        await query.answer(text=shown.get("answer") or None)
    except Exception as exc:  # a decision can outlast the query: the edit below still shows it
        logger.info("[Telegram] staged-action press not answered: %s", type(exc).__name__)
    if shown.get("edit"):
        try:
            await query.edit_message_text(text=shown["edit"])
        except Exception as exc:
            logger.warning("[Telegram] staged-action card not edited: %s", type(exc).__name__)


def register(app: Any, channel: TelegramChannel) -> None:
    """Add the ``callback_query`` handler to the adapter's application.

    ``block=False``: a press makes up to two calls to atlas-mcp, and a webhook
    held that long is redelivered by Telegram. A handler that cannot be added
    never stops the channel from starting: presses then go unanswered and the
    actions wait in the web Inbox.
    """

    async def _handler(update: Any, context: Any) -> None:
        del context
        await on_press(channel, update)

    try:
        from telegram.ext import CallbackQueryHandler

        app.add_handler(CallbackQueryHandler(_handler, block=False))
    except Exception as exc:
        logger.warning("[Telegram] staged-action presses not registered: %s", type(exc).__name__)

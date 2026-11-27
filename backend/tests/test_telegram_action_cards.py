"""[argus patch #178] Telegram approval cards for staged actions: the fork's hook.

The card's text, the owner check and the calls to atlas-mcp belong to the Argus
overlay module ``argus_telegram_actions``, which is not in this repository. A
stand-in with the same three functions is installed here; what these tests lock
is the hook: the ids ride a Telegram reply's metadata (a scheduled run's as
well), the cards follow the final reply, a press is answered and the card
edited, and nothing changes when the overlay is missing or raises.
"""

from __future__ import annotations

import asyncio
import json
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_channel_tool_call_markup import _stream

from app.channels import _telegram_actions
from app.channels._telegram_actions import STAGED_ACTIONS_KEY, on_press, register, send_cards, stamp_staged_actions
from app.channels.message_bus import MessageBus, OutboundMessage
from app.channels.telegram import TelegramChannel

OWNER = 8725938099
FIRST, SECOND, EARLIER = "01JDK3V7Q0A8N4W2R6T9Y5H1ZC", "01JDK3V7Q0A8N4W2R6T9Y5H1ZD", "01JDK3V7Q0A8N4W2R6T9Y5H1ZB"
BUTTONS = {"inline_keyboard": [[{"text": "Approve", "callback_data": f"a:{FIRST}"}, {"text": "Skip", "callback_data": f"s:{FIRST}"}]]}
PAYLOAD = "body: 5 < 6 & <b>not bold</b>"


def _staged(action_id: str, status: str = "staged") -> dict:
    return {"type": "tool", "name": "atlas_action_stage", "content": json.dumps({"status": status, "id": action_id})}


def _overlay(monkeypatch, **over) -> ModuleType:
    """The overlay's three functions, as its contract gives them."""

    def staged_action_ids(tool_results):
        found = [json.loads(m["content"]) for m in tool_results if m.get("name") == "atlas_action_stage"]
        return [a["id"] for a in found if a.get("status") == "staged"]

    module = ModuleType("argus_telegram_actions")
    module.staged_action_ids = over.get("staged_action_ids", staged_action_ids)
    module.cards = over.get("cards", AsyncMock(return_value=[{"text": "part one", "reply_markup": None}, {"text": PAYLOAD, "reply_markup": BUTTONS}]))
    module.on_callback = over.get("on_callback", AsyncMock(return_value={"answer": "Approved and carried out.", "edit": "Approved and carried out.\nurl: https://example.test/1"}))
    monkeypatch.setitem(sys.modules, "argus_telegram_actions", module)
    return module


def _no_overlay(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "argus_telegram_actions", None)  # `import` raises ImportError


def _turn(*tool_messages: dict, final: str = "I staged the reply for you to approve.") -> list[dict]:
    return [
        {"type": "human", "content": "an earlier ask"},
        _staged(EARLIER),
        {"type": "ai", "content": "Staged."},
        {"type": "human", "content": "Draft a reply to the invoice thread"},
        {"type": "ai", "content": "", "tool_calls": [{"name": "atlas_action_stage", "args": {}, "id": "call_1", "type": "tool_call"}]},
        *tool_messages,
        {"type": "ai", "content": final, "tool_calls": []},
    ]


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _channel(allowed=(OWNER,)):
    ch = TelegramChannel(bus=MessageBus(), config={"bot_token": "t", "allowed_users": list(allowed)})
    app = MagicMock()
    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=1)
    app.bot = bot
    ch._application = app
    return ch, bot


def _final(ids=(FIRST,), **kw) -> OutboundMessage:
    return OutboundMessage(channel_name="telegram", chat_id=str(OWNER), thread_id="th", text="Staged it.", metadata={STAGED_ACTIONS_KEY: list(ids)} if ids else {}, **kw)


# -- the manager: the turn's staged ids ride the final reply ------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("unattended", [True, False])
async def test_a_scheduled_runs_telegram_message_carries_the_ids_it_staged(monkeypatch, tmp_path, unattended):
    _overlay(monkeypatch)
    finals, _ = await _stream(monkeypatch, tmp_path, messages=_turn(_staged(FIRST), _staged(SECOND)), unattended=unattended)
    [final] = finals
    assert final.channel_name == "telegram" and final.text == "I staged the reply for you to approve."
    assert final.metadata[STAGED_ACTIONS_KEY] == [FIRST, SECOND], "this turn's two, never the earlier turn's"


@pytest.mark.asyncio
async def test_a_turn_that_staged_nothing_and_a_gateway_without_the_overlay_send_the_reply_as_before(monkeypatch, tmp_path):
    _overlay(monkeypatch)
    [plain], _ = await _stream(monkeypatch, tmp_path, messages=_turn(_staged(FIRST, status="refused")), unattended=True)
    assert STAGED_ACTIONS_KEY not in plain.metadata
    _no_overlay(monkeypatch)
    [bare], _ = await _stream(monkeypatch, tmp_path, messages=_turn(_staged(FIRST)), unattended=True)
    assert STAGED_ACTIONS_KEY not in bare.metadata and bare.text == "I staged the reply for you to approve."


def test_only_telegram_is_stamped_and_an_overlay_that_raises_changes_nothing(monkeypatch):
    _overlay(monkeypatch)
    turn = _turn(_staged(FIRST))[3:]  # the call sites pass the current turn's messages only
    assert stamp_staged_actions({"k": 1}, "slack", turn) == {"k": 1}
    kept = {"k": 1}
    assert stamp_staged_actions(kept, "telegram", turn) is kept and kept == {"k": 1, STAGED_ACTIONS_KEY: [FIRST]}
    _overlay(monkeypatch, staged_action_ids=MagicMock(side_effect=RuntimeError("boom")))
    assert stamp_staged_actions({"k": 1}, "telegram", turn) == {"k": 1}


# -- the sender: the cards follow the final reply -----------------------------------------------
def test_the_cards_follow_the_final_reply_as_plain_messages_with_the_buttons_on_the_last_part(monkeypatch):
    overlay = _overlay(monkeypatch)
    ch, bot = _channel()
    _run(ch.send(_final()))
    reply, one, two = [c.kwargs for c in bot.send_message.await_args_list]
    assert reply["parse_mode"] == "HTML" and "Staged it." in reply["text"]
    overlay.cards.assert_awaited_once_with([FIRST], frozenset({OWNER}), OWNER)
    assert one == {"chat_id": OWNER, "text": "part one", "reply_markup": None}
    assert two["text"] == PAYLOAD and "parse_mode" not in two, "a payload is shown verbatim, never parsed as HTML"
    [[approve, skip]] = two["reply_markup"].inline_keyboard
    assert (approve.text, approve.callback_data, skip.text, skip.callback_data) == ("Approve", f"a:{FIRST}", "Skip", f"s:{FIRST}")


def test_a_partial_a_reply_without_ids_and_a_missing_overlay_send_no_card(monkeypatch):
    overlay = _overlay(monkeypatch)
    ch, bot = _channel()
    _run(ch.send(_final(is_final=False)))
    _run(ch.send(_final(ids=())))
    assert overlay.cards.await_count == 0 and bot.send_message.await_count == 2
    _no_overlay(monkeypatch)
    _run(ch.send(_final()))
    assert bot.send_message.await_count == 3, "the reply alone"


def test_a_failing_overlay_or_a_failing_card_never_breaks_the_reply(monkeypatch):
    _overlay(monkeypatch, cards=AsyncMock(side_effect=RuntimeError("boom")))
    ch, bot = _channel()
    _run(ch.send(_final()))
    assert bot.send_message.await_count == 1
    _overlay(monkeypatch)
    ch, bot = _channel()
    bot.send_message.side_effect = [SimpleNamespace(message_id=1), RuntimeError("telegram is down")]
    _run(ch.send(_final()))
    assert bot.send_message.await_count == 2, "the reply, then one card that failed: not retried, nothing raised"


# -- the adapter: a press is handed over, answered, and the card edited --------------------------
def _press(data=f"a:{FIRST}"):
    query = SimpleNamespace(answer=AsyncMock(), edit_message_text=AsyncMock())
    body = {"update_id": 7, "callback_query": {"id": "q", "data": data, "from": {"id": OWNER}, "message": {"chat": {"id": OWNER, "type": "private"}}}}
    return SimpleNamespace(callback_query=query, to_dict=lambda: body), query, body


def test_a_press_goes_to_the_overlay_is_answered_and_the_card_becomes_the_receipt(monkeypatch):
    overlay = _overlay(monkeypatch)
    ch, _ = _channel()
    update, query, body = _press()
    _run(on_press(ch, update))
    overlay.on_callback.assert_awaited_once_with(body, frozenset({OWNER}))
    query.answer.assert_awaited_once_with(text="Approved and carried out.")
    query.edit_message_text.assert_awaited_once_with(text="Approved and carried out.\nurl: https://example.test/1")


def test_a_press_nobody_may_act_on_is_answered_and_the_card_is_left_alone(monkeypatch):
    _overlay(monkeypatch, on_callback=AsyncMock(return_value={"answer": "Only the stack owner can decide this.", "edit": None}))
    ch, _ = _channel()
    update, query, _ = _press()
    _run(on_press(ch, update))
    query.answer.assert_awaited_once_with(text="Only the stack owner can decide this.")
    query.edit_message_text.assert_not_awaited()


@pytest.mark.parametrize("broken", ["missing", "raises"])
def test_without_the_overlay_or_when_it_raises_the_press_is_still_answered(monkeypatch, broken):
    if broken == "missing":
        _no_overlay(monkeypatch)
    else:
        _overlay(monkeypatch, on_callback=AsyncMock(side_effect=RuntimeError("boom")))
    ch, _ = _channel()
    update, query, _ = _press()
    _run(on_press(ch, update))
    query.answer.assert_awaited_once_with(text=None)
    query.edit_message_text.assert_not_awaited()


def test_an_answer_that_comes_too_late_still_edits_the_card_and_an_update_without_a_press_is_ignored(monkeypatch):
    overlay = _overlay(monkeypatch)
    ch, _ = _channel()
    update, query, _ = _press()
    query.answer.side_effect = RuntimeError("query is too old")
    _run(on_press(ch, update))
    query.edit_message_text.assert_awaited_once()
    _run(on_press(ch, SimpleNamespace(callback_query=None)))
    assert overlay.on_callback.await_count == 1


def test_the_adapter_registers_one_callback_query_handler_that_does_not_block():
    from telegram.ext import CallbackQueryHandler

    ch, _ = _channel()
    app = MagicMock()
    register(app, ch)
    [handler] = [c.args[0] for c in app.add_handler.call_args_list]
    assert isinstance(handler, CallbackQueryHandler) and handler.block is False
    # A handler that cannot be added never stops the channel from starting.
    app.add_handler.side_effect = RuntimeError("boom")
    register(app, ch)


def test_the_three_seams_call_the_hook():
    """A sync that drops one of the three call sites must fail here, not in a citizen's chat."""
    import inspect

    from app.channels import _telegram_sender, manager, telegram

    assert inspect.getsource(manager).count("stamp_staged_actions(_response_metadata_with_clarification(") == 2
    assert "await send_cards(channel, bot, chat_id, msg)" in inspect.getsource(_telegram_sender.send)
    assert "_telegram_actions.register(app, self)" in inspect.getsource(telegram.TelegramChannel.start)
    assert send_cards is _telegram_sender.send_cards and _telegram_actions.STAGED_ACTIONS_KEY == "argus_staged_actions"

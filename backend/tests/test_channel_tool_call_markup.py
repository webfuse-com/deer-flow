"""[argus patch #152] A final reply that is raw tool-call markup is not delivered.

atlas-vipin, Sat 2026-10-17 23:51 CEST (run 11f2d321, Chronos 45146): the
hourly meeting-prep poll called ``function=calendar_list_events`` (not a valid
tool), then answered with the literal text of a tool call. Delivery sent that
text to Telegram and Chronos recorded the run as delivered. The same happened
on 2026-09-29 (Chronos 36546). The literals below are those runs' final AI
texts, verbatim.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from _router_auth_helpers import make_authed_test_app
from langgraph_sdk.client import LangGraphClient

from app.channels._final_reply import TOOL_CALL_MARKUP_NOTICE, reply_without_tool_call_markup, strip_tool_call_markup
from app.channels.manager import ChannelManager
from app.channels.message_bus import InboundMessage, MessageBus
from app.channels.store import JsonChannelStore
from app.gateway.routers import thread_runs
from deerflow.runtime import DisconnectMode, RunRecord, RunStatus
from deerflow.runtime.stream_bridge.memory import MemoryStreamBridge

REPORT_URL = "http://argus-scheduler:8000/api/runs/45146/output"

# deerflow_atlas_vipin run 11f2d321 seq 9 and run 5f9b12d9 seq 23.
VIPIN_1017 = "\n\n<tool_call>\n<|im_start|>function_calendar_list_events>\n<parameter=days>\n1\n</parameter>\n</function>\n</tool_call>"
VIPIN_0929 = '\n\n<tool_call>\n<|im_start|>function>\n<parameter name="q">00007073\n</parameter>\n</function>\n</tool_call>'
# deerflow_atlas_nicholas run 1961ed68 seq 24 (the Qwen ``function=`` form).
QWEN_FUNCTION_EQUALS = (
    "\n\n<tool_call>\n<|im_start|>function=bash>\n<parameter=command>\ncd /mnt/apps/kairos && sed -n '1,55p' app.js && echo \"=== api/update ===\" && sed -n '130,220p' app.js\n</parameter>\n"
    "<parameter=description>\nRead app.js header and data layer\n</parameter>\n</function>\n</tool_call>\n"
)
# deerflow_atlas_quinten run cfefe2ba seq 25: a whole document inside an unparsed write_file call.
QUINTEN_WRITE_FILE = (
    "\n\n<tool_call>\n<functionfunction=write_file>\n<parameter=path>\n/mnt/user-data/outputs/den-bosch-demo-script-29sep.md\n</parameter>\n<parameter=content>\n"
    "# Demo script: Gemeente 's-Hertogenbosch\n**Tue 29 Sep, 14:00-14:45 CEST**\n\n- You are the AE.\n\n</parameter>\n</function>\n</tool_call>"
)
# deerflow_atlas_vamsi run 1e3e7974 seq 30: prose that names the wrapper inline.
VAMSI_PROSE_MENTION = "The calibrator correctly identifies both failure shapes. One last check on the parser against the actual Qwen emit formats (the `<tool_call>` wrapper and a bare JSON with think text around it):"

BRIEF = "*Morning brief*\n\n*Inbox*\n" + "\n".join(f"• Item {i}: something that needs an action today" for i in range(6))


@pytest.mark.parametrize("text", [VIPIN_1017, VIPIN_0929, QWEN_FUNCTION_EQUALS, QUINTEN_WRITE_FILE, "\n\n\n<tool_call>\n"])
def test_raw_tool_calls_strip_to_nothing(text):
    assert strip_tool_call_markup(text, unattended=True) == ("", True)
    assert strip_tool_call_markup(text, unattended=False) == ("", True)


@pytest.mark.parametrize(
    "text",
    [
        '<function_calls>\n<invoke name="calendar_list_events">\n<parameter name="days">1</parameter>\n</invoke>\n</function_calls>',
        "<function=calendar_list_events>\n<parameter=days>\n1\n</parameter>\n</function>",
        '<|im_start|>assistant\n<tool_call>\n{"name": "x", "arguments": {}}\n</tool_call><|im_end|>',
        "<|tool_calls_section_begin|><|tool_call_begin|>functions.calendar_list_events:0<|tool_call_argument_begin|>{}<|tool_call_end|><|tool_calls_section_end|>",
    ],
)
def test_other_model_families_strip_too(text):
    assert strip_tool_call_markup(text, unattended=True) == ("", True)


def test_text_around_the_markup_is_kept():
    text = f"Here is the brief.\n{VIPIN_1017}\nThat is all."
    assert strip_tool_call_markup(text, unattended=False) == ("Here is the brief.\n\nThat is all.", True)


@pytest.mark.parametrize(
    "text",
    [
        VAMSI_PROSE_MENTION,
        'Qwen emits this shape:\n\n```\n<tool_call>\n{"name": "bash", "arguments": {"command": "ls"}}\n</tool_call>\n```\n\nThe parser reads the JSON between the tags.',
        "Use <function=name> tags only inside the template, never in prose.",
        BRIEF,
        "",
    ],
)
def test_prose_and_code_blocks_about_tool_calls_are_untouched(text):
    assert strip_tool_call_markup(text, unattended=True) == (text, False)


@pytest.mark.parametrize(
    "text",
    [
        '{"name": "calendar_list_events", "arguments": {"days": 1}}',
        '```json\n{"name": "calendar_list_events", "parameters": {"days": 1}}\n```',
        '[{"type": "function", "function": {"name": "gmail_list_threads", "arguments": "{}"}}]',
        '{"tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]}',
    ],
)
def test_a_json_tool_call_blob_is_markup_only_on_unattended_runs(text):
    assert strip_tool_call_markup(text, unattended=True) == ("", True)
    # Someone in a chat may have asked for exactly this JSON.
    assert strip_tool_call_markup(text, unattended=False) == (text, False)


@pytest.mark.parametrize("text", ['{"status": "ok", "name": "nightly"}', '{"name": "Harel Group", "mrr": 500}', "[1, 2, 3]", '{"name": "x", "arguments": {}, "extra": 1}'])
def test_other_json_is_not_a_tool_call(text):
    assert strip_tool_call_markup(text, unattended=True) == (text, False)


def test_the_delivery_decision():
    # Unattended: refused unless something substantive is left.
    assert reply_without_tool_call_markup(VIPIN_1017, unattended=True, has_artifacts=False, thread_id="t").refused is True
    short = reply_without_tool_call_markup(f"Checking the calendar.{VIPIN_1017}", unattended=True, has_artifacts=False, thread_id="t")
    assert short.refused is True
    long = reply_without_tool_call_markup(f"{BRIEF}\n{VIPIN_1017}", unattended=True, has_artifacts=False, thread_id="t")
    assert (long.text, long.found, long.refused) == (BRIEF, True, False)
    files = reply_without_tool_call_markup(VIPIN_1017, unattended=True, has_artifacts=True, thread_id="t")
    assert (files.text, files.refused) == ("", False)
    # Interactive: whatever is left, or a one-line notice instead of the markup.
    assert reply_without_tool_call_markup(VIPIN_1017, unattended=False, has_artifacts=False, thread_id="t").text == TOOL_CALL_MARKUP_NOTICE
    kept = reply_without_tool_call_markup(f"Checking the calendar.{VIPIN_1017}", unattended=False, has_artifacts=False, thread_id="t")
    assert (kept.text, kept.refused) == ("Checking the calendar.", False)
    # No markup: the text is returned as it is.
    plain = reply_without_tool_call_markup("\n\nAll clear.", unattended=True, has_artifacts=False, thread_id="t")
    assert (plain.text, plain.found, plain.refused) == ("\n\nAll clear.", False, False)


# ---------------------------------------------------------------------------
# Through the real delivery paths


async def _noop():
    return None


def _vipin_run(final: str) -> list[dict]:
    return [
        {"type": "human", "content": "Check my calendar for meetings starting in the next 15-20 minutes"},
        {"type": "ai", "content": "\n\n", "tool_calls": [{"name": "function=calendar_list_events", "args": {"days": "1"}, "id": "call_432a63d2b0aa467097ee415a", "type": "tool_call"}]},
        {"type": "tool", "name": "function=calendar_list_events", "content": "Error: function=calendar_list_events is not a valid tool, try one of [...]", "status": "error"},
        {"type": "ai", "content": final, "tool_calls": []},
    ]


async def _stream(monkeypatch, tmp_path, *, messages: list[dict], unattended: bool):
    bridge = MemoryStreamBridge()
    record = RunRecord(run_id="11f2d321", thread_id="48c5f843", assistant_id=None, status=RunStatus.success, on_disconnect=DisconnectMode.continue_)

    async def start_run(*args, **kwargs):
        await bridge.publish(record.run_id, "values", {"messages": messages})
        await bridge.publish_end(record.run_id)
        return record

    monkeypatch.setattr(thread_runs, "start_run", start_run)
    app = make_authed_test_app()
    app.include_router(thread_runs.router)
    app.state.stream_bridge = bridge
    app.state.run_manager = SimpleNamespace(get=AsyncMock(return_value=record))
    bus = MessageBus()
    manager = ChannelManager(bus=bus, store=JsonChannelStore(path=tmp_path / "channels.json"))
    inbound = InboundMessage(
        channel_name="telegram",
        chat_id="8725938099",
        user_id="user-1",
        text="Check my calendar for meetings starting in the next 15-20 minutes",
        metadata={"message_id": "m1", "source": "playbook", "schedule_id": "meeting-prep"},
        unattended=unattended,
        report_url=REPORT_URL if unattended else None,
    )
    outbound = []
    bus.subscribe_outbound(lambda m: outbound.append(m) or _noop())
    with patch("app.channels.manager.report_delivery", new=AsyncMock(return_value=True)) as report:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test/api") as http:
            await manager._handle_streaming_chat(LangGraphClient(http), inbound, "48c5f843", "lead_agent", {}, {}, {"role": "user", "content": "poll"})
    return [m for m in outbound if m.is_final], report


@pytest.mark.asyncio
async def test_the_saturday_night_markup_is_not_sent_and_the_run_is_failed(monkeypatch, tmp_path):
    finals, report = await _stream(monkeypatch, tmp_path, messages=_vipin_run(VIPIN_1017), unattended=True)
    assert finals == []
    assert [call.kwargs["status"] for call in report.await_args_list] == ["failed"]
    assert "tool-call markup" in report.await_args_list[0].kwargs["error"]


@pytest.mark.asyncio
async def test_a_brief_with_trailing_markup_is_sent_without_it(monkeypatch, tmp_path):
    finals, report = await _stream(monkeypatch, tmp_path, messages=_vipin_run(f"{BRIEF}\n{VIPIN_1017}"), unattended=True)
    assert [m.text for m in finals] == [BRIEF]
    assert [call.kwargs["status"] for call in report.await_args_list] == ["delivered"]
    assert report.await_args_list[0].kwargs["message_text"] == BRIEF


@pytest.mark.asyncio
async def test_an_interactive_chat_gets_a_notice_instead_of_markup(monkeypatch, tmp_path):
    finals, report = await _stream(monkeypatch, tmp_path, messages=_vipin_run(VIPIN_1017), unattended=False)
    assert [m.text for m in finals] == [TOOL_CALL_MARKUP_NOTICE]
    report.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_non_streaming_path_refuses_it_too(tmp_path):
    bus = MessageBus()
    manager = ChannelManager(bus=bus, store=JsonChannelStore(path=tmp_path / "channels.json"))
    client = SimpleNamespace(runs=SimpleNamespace(wait=AsyncMock(return_value={"messages": _vipin_run(VIPIN_0929)})))
    outbound = []
    bus.subscribe_outbound(lambda m: outbound.append(m) or _noop())
    inbound = InboundMessage(channel_name="slack", chat_id="C1", user_id="u1", text="poll", unattended=True, report_url=REPORT_URL)
    with patch("app.channels.manager.report_delivery", new=AsyncMock(return_value=True)) as report, patch.object(ChannelManager, "_channel_supports_streaming", return_value=False):
        await manager._handle_chat_on_thread(client, inbound, "48c5f843")
    assert outbound == []
    assert report.await_args.kwargs["status"] == "failed"

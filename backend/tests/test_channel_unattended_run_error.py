"""[argus patch #128] A failed Telegram run is an error, not an empty answer.

The Telegram branch of ``_handle_streaming_chat`` skipped ``error`` SSE frames,
so a scheduled run that died (GraphRecursionError on atlas-ashish, 2026-10-13)
was reported to Chronos as ``silent`` and the citizen heard nothing.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from _router_auth_helpers import make_authed_test_app
from langgraph_sdk.client import LangGraphClient

from app.channels.manager import ChannelManager
from app.channels.message_bus import InboundMessage, MessageBus
from app.channels.store import JsonChannelStore
from app.gateway.routers import thread_runs
from deerflow.runtime import DisconnectMode, RunRecord, RunStatus
from deerflow.runtime.stream_bridge.memory import MemoryStreamBridge

REPORT_URL = "http://argus-scheduler:8000/api/runs/42623/output"


async def _run(monkeypatch, tmp_path, *, unattended: bool, error_payload, partial: str | None = None):
    bridge = MemoryStreamBridge()
    record = RunRecord(
        run_id="r1",
        thread_id="thread-1",
        assistant_id=None,
        status=RunStatus.error if error_payload is not None else RunStatus.success,
        on_disconnect=DisconnectMode.continue_,
        error="boom" if error_payload is not None else None,
    )

    async def start_run(*args, **kwargs):
        await bridge.publish(record.run_id, "values", {"messages": [{"type": "ai", "content": partial}] if partial else []})
        if error_payload is not None:
            await bridge.publish(record.run_id, "error", error_payload)
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
        chat_id="8974705677",
        user_id="user-1",
        text="Refresh the dashboard",
        metadata={"message_id": "m1", "source": "playbook", "schedule_id": "surfly-dashboard-refresh"},
        unattended=unattended,
        report_url=REPORT_URL if unattended else None,
    )
    outbound = []
    bus.subscribe_outbound(lambda m: outbound.append(m) or _noop())
    with patch("app.channels.manager.report_delivery", new=AsyncMock(return_value=True)) as report:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test/api") as http:
            await manager._handle_streaming_chat(LangGraphClient(http), inbound, "thread-1", "lead_agent", {}, {}, {"role": "user", "content": "Refresh"})
    return [m for m in outbound if m.is_final], report


async def _noop():
    return None


@pytest.mark.asyncio
async def test_failed_schedule_tells_the_citizen_and_reports_failed(monkeypatch, tmp_path):
    finals, report = await _run(
        monkeypatch,
        tmp_path,
        unattended=True,
        error_payload={"name": "GraphRecursionError", "message": "Recursion limit of 1000 reached"},
    )
    assert len(finals) == 1
    text = finals[0].text
    assert "'surfly-dashboard-refresh'" in text and "step limit" in text
    statuses = [call.kwargs["status"] for call in report.await_args_list]
    assert statuses == ["failed"]
    assert "GraphRecursionError" in report.await_args_list[0].kwargs["error"]


@pytest.mark.asyncio
async def test_clean_empty_schedule_stays_silent(monkeypatch, tmp_path):
    finals, report = await _run(monkeypatch, tmp_path, unattended=True, error_payload=None)
    assert finals == []
    assert [call.kwargs["status"] for call in report.await_args_list] == ["silent"]


@pytest.mark.asyncio
async def test_interactive_telegram_error_is_shown_as_an_error(monkeypatch, tmp_path):
    finals, report = await _run(monkeypatch, tmp_path, unattended=False, error_payload={"name": "RuntimeError", "message": "Model failed."})
    assert len(finals) == 1
    assert finals[0].text == "An error occurred while processing your request. Please try again."


@pytest.mark.asyncio
async def test_partial_text_is_kept_and_still_reported_failed(monkeypatch, tmp_path):
    finals, report = await _run(
        monkeypatch,
        tmp_path,
        unattended=True,
        error_payload={"name": "GraphRecursionError", "message": "limit"},
        partial="Dashboard data built; committing next.",
    )
    assert finals[0].text == "Dashboard data built; committing next."
    assert [call.kwargs["status"] for call in report.await_args_list] == ["failed"]

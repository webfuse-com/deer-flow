"""[argus patch #45] Delivery-outcome callback (gateway -> Chronos).

Locks the contract of app.channels._delivery_report.report_delivery — the
POST payload shape, the internal-token header, the retry-once-never-raise
behavior — and the manager's _report_unattended_outcome gate (unattended +
report_url only).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import app.channels._delivery_report as delivery_report
import app.gateway.internal_auth as internal_auth
from app.channels._delivery_report import report_delivery
from app.channels.manager import ChannelManager
from app.channels.message_bus import InboundMessage

REPORT_URL = "http://argus-scheduler:8000/api/runs/7/output"
TEST_TOKEN = "test-internal-token"


@pytest.fixture(autouse=True)
def _pin_internal_token():
    with patch.object(internal_auth, "_INTERNAL_AUTH_TOKEN", TEST_TOKEN):
        yield


@pytest.fixture(autouse=True)
def _no_retry_sleep():
    with patch.object(delivery_report.asyncio, "sleep", AsyncMock()):
        yield


class _CapturingClient:
    """Stand-in for httpx.AsyncClient capturing posts; scriptable responses."""

    def __init__(self, outcomes):
        # outcomes: list of "ok", "http500", or an Exception instance
        self.outcomes = list(outcomes)
        self.posts: list[dict] = []

    def __call__(self, *args, **kwargs):  # constructor stand-in
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, *, json=None, headers=None):
        self.posts.append({"url": url, "json": json, "headers": headers})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        req = httpx.Request("POST", url)
        if outcome == "http500":
            resp = httpx.Response(500, request=req)
        else:
            resp = httpx.Response(200, request=req)
        return resp


class TestReportDelivery:
    @pytest.mark.asyncio
    async def test_posts_payload_with_internal_token(self):
        fake = _CapturingClient(["ok"])
        with patch.object(delivery_report.httpx, "AsyncClient", fake):
            ok = await report_delivery(
                REPORT_URL, status="delivered", channel="telegram",
                chat_id="8726302666", message_text="Meeting brief ...",
            )
        assert ok is True
        assert len(fake.posts) == 1
        post = fake.posts[0]
        assert post["url"] == REPORT_URL
        assert post["headers"][internal_auth.INTERNAL_AUTH_HEADER_NAME] == TEST_TOKEN
        body = post["json"]
        assert body["status"] == "delivered"
        assert body["channel"] == "telegram"
        assert body["chat_id"] == "8726302666"
        assert body["message_text"] == "Meeting brief ..."
        assert "delivered_at" in body  # ISO stamp only on delivered

    @pytest.mark.asyncio
    async def test_silent_payload_is_minimal(self):
        fake = _CapturingClient(["ok"])
        with patch.object(delivery_report.httpx, "AsyncClient", fake):
            ok = await report_delivery(REPORT_URL, status="silent", channel="telegram", chat_id="1")
        assert ok is True
        body = fake.posts[0]["json"]
        assert body["status"] == "silent"
        assert "message_text" not in body
        assert "delivered_at" not in body

    @pytest.mark.asyncio
    async def test_failed_payload_carries_error(self):
        fake = _CapturingClient(["ok"])
        with patch.object(delivery_report.httpx, "AsyncClient", fake):
            await report_delivery(REPORT_URL, status="failed", error="RuntimeError('boom')")
        body = fake.posts[0]["json"]
        assert body["status"] == "failed"
        assert body["error"] == "RuntimeError('boom')"

    @pytest.mark.asyncio
    async def test_none_url_is_noop(self):
        fake = _CapturingClient([])
        with patch.object(delivery_report.httpx, "AsyncClient", fake):
            ok = await report_delivery(None, status="silent")
        assert ok is False
        assert fake.posts == []

    @pytest.mark.asyncio
    async def test_retries_once_then_succeeds(self):
        fake = _CapturingClient([httpx.ConnectError("down"), "ok"])
        with patch.object(delivery_report.httpx, "AsyncClient", fake):
            ok = await report_delivery(REPORT_URL, status="silent")
        assert ok is True
        assert len(fake.posts) == 2

    @pytest.mark.asyncio
    async def test_gives_up_after_two_attempts_never_raises(self):
        fake = _CapturingClient([httpx.ConnectError("down"), "http500"])
        with patch.object(delivery_report.httpx, "AsyncClient", fake):
            ok = await report_delivery(REPORT_URL, status="delivered")
        assert ok is False
        assert len(fake.posts) == 2  # exactly one retry, no loop

    @pytest.mark.asyncio
    async def test_long_message_text_is_capped(self):
        fake = _CapturingClient(["ok"])
        with patch.object(delivery_report.httpx, "AsyncClient", fake):
            await report_delivery(REPORT_URL, status="delivered", message_text="x" * 20000)
        text = fake.posts[0]["json"]["message_text"]
        assert len(text) < 20000
        assert text.endswith("…[truncated]")


def _msg(*, unattended=True, report_url=REPORT_URL) -> InboundMessage:
    return InboundMessage(
        channel_name="telegram", chat_id="8726302666", user_id="u1",
        text="playbook prompt", unattended=unattended, report_url=report_url,
    )


class TestManagerOutcomeGate:
    """_report_unattended_outcome only reports for unattended fires that
    carried a report_url; interactive turns and pre-#45 fires are no-ops."""

    def _manager(self) -> ChannelManager:
        mgr = ChannelManager.__new__(ChannelManager)  # gate needs no manager state
        return mgr

    @pytest.mark.asyncio
    async def test_reports_for_unattended_with_url(self):
        mgr = self._manager()
        with patch.object(delivery_report, "report_delivery", AsyncMock()) as rep, \
             patch("app.channels.manager.report_delivery", rep):
            await mgr._report_unattended_outcome(_msg(), status="silent")
        rep.assert_awaited_once()
        kwargs = rep.await_args.kwargs
        assert kwargs["status"] == "silent"
        assert kwargs["channel"] == "telegram"
        assert kwargs["chat_id"] == "8726302666"

    @pytest.mark.asyncio
    async def test_noop_without_report_url(self):
        mgr = self._manager()
        with patch("app.channels.manager.report_delivery", AsyncMock()) as rep:
            await mgr._report_unattended_outcome(_msg(report_url=None), status="silent")
        rep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_noop_for_interactive_turn(self):
        mgr = self._manager()
        with patch("app.channels.manager.report_delivery", AsyncMock()) as rep:
            await mgr._report_unattended_outcome(_msg(unattended=False), status="delivered")
        rep.assert_not_awaited()

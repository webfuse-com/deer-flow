"""[argus patch #173] ``channel: notify`` resolves the person's preference at fire time.

The Slack router holds the per-stack preference and answers ``argus.owner``
with an ordered channel list. The fire route executes it: ``["slack"]``
delivers like ``channel: slack`` (#172), ``["telegram"]`` takes today's
Telegram path, ``["slack", "telegram"]`` delivers on Slack and mirrors the
final answer text to the Telegram root chat. A router that does not answer
falls back to Telegram; Telegram with no Telegram channel running is a 409.
"""

from __future__ import annotations

import asyncio
import functools
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from slack_sdk.errors import SlackApiError

import app.gateway.internal_auth as internal_auth
import app.gateway.routers.playbooks as playbooks
from app.channels._delivery_outcome import MIRROR_KEY, mirror_outbounds
from app.channels.message_bus import InboundMessage, MessageBus
from app.channels.slack import SlackChannel, SlackOwner
from app.channels.store import JsonChannelStore

DM = "D-owner"
OWNER = "U-owner"
TELEGRAM_CHAT = "8726302666"
TOKEN_HEADER = internal_auth.INTERNAL_AUTH_HEADER_NAME
TEST_TOKEN = "test-internal-token"
SCHEDULE_ID = "morning-briefing"
REPORT_URL = "http://argus-scheduler:8000/api/runs/9/output"
TELEGRAM_ROWS = [
    {"channel_name": "telegram", "chat_id": TELEGRAM_CHAT, "user_id": TELEGRAM_CHAT},
    {"channel_name": "telegram", "chat_id": TELEGRAM_CHAT, "user_id": TELEGRAM_CHAT, "topic_id": "sched:morning-briefing"},
]


@pytest.fixture(autouse=True)
def _pin_internal_token():
    with patch.object(internal_auth, "_INTERNAL_AUTH_TOKEN", TEST_TOKEN):
        yield


@pytest.fixture()
def playbook_dir(tmp_path: Path):
    (tmp_path / f"{SCHEDULE_ID}.md").write_text("Good morning. Today is {{TODAY}}.", encoding="utf-8")
    with patch.object(playbooks, "PLAYBOOK_DIR", tmp_path):
        yield tmp_path


def owner(*channels: str, enabled: bool = True, dm: str = DM) -> SlackOwner:
    return SlackOwner(enabled=enabled, user_id=OWNER if dm else "", dm=dm, channels=tuple(channels))


def fake_slack(answer: SlackOwner | None, error: str = "") -> MagicMock:
    slack = MagicMock()
    slack.uses_relay_transport = True
    slack.owner_conversation = AsyncMock(return_value=(answer, error))
    return slack


def fake_service(*, slack=None, telegram: bool = True, telegram_rows: list[dict] | None = None) -> MagicMock:
    channels = {"slack": slack, "telegram": MagicMock() if telegram else None}
    rows = {"telegram": TELEGRAM_ROWS if telegram_rows is None else telegram_rows, "slack": []}
    service = MagicMock()
    service.get_channel.side_effect = channels.get
    service.store.list_entries = AsyncMock(side_effect=lambda channel=None: rows.get(channel, []))
    service.store.remove = AsyncMock(return_value=True)
    service.bus.publish_inbound = AsyncMock()
    return service


async def fire(service, body: dict) -> httpx.Response:
    app = FastAPI()
    app.include_router(playbooks.router)
    with patch("app.channels.service.get_channel_service", return_value=service):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as http:
            return await http.post(f"/api/playbooks/{SCHEDULE_ID}/fire", json=body, headers={TOKEN_HEADER: TEST_TOKEN})


def published(service) -> InboundMessage:
    service.bus.publish_inbound.assert_awaited_once()
    return service.bus.publish_inbound.await_args.args[0]


# -- the fire route ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_slack_delivers_like_channel_slack(playbook_dir):
    slack = fake_slack(owner("slack"))
    service = fake_service(slack=slack)
    resp = await fire(service, {"channel": "notify", "topic_id": f"sched:{SCHEDULE_ID}", "report_url": REPORT_URL})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["channel"] == "slack" and data["mirror"] == [] and data["resolution"] == "argus.owner: slack"
    msg = published(service)
    assert (msg.channel_name, msg.chat_id, msg.user_id) == ("slack", DM, OWNER)
    assert msg.report_url == REPORT_URL and msg.unattended is True
    assert MIRROR_KEY not in msg.metadata
    # One router call per fire: the resolution's answer is reused for the DM.
    slack.owner_conversation.assert_awaited_once()


@pytest.mark.asyncio
async def test_notify_telegram_takes_todays_telegram_path(playbook_dir):
    service = fake_service(slack=fake_slack(owner("telegram")))
    resp = await fire(service, {"channel": "notify", "topic_id": f"sched:{SCHEDULE_ID}", "history": "none"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["channel"] == "telegram" and resp.json()["mirror"] == []
    msg = published(service)
    assert (msg.channel_name, msg.chat_id) == ("telegram", TELEGRAM_CHAT)
    assert msg.topic_id == f"sched:{SCHEDULE_ID}"
    assert "tier" not in msg.metadata and MIRROR_KEY not in msg.metadata
    service.store.remove.assert_awaited_once_with("telegram", TELEGRAM_CHAT, f"sched:{SCHEDULE_ID}")


@pytest.mark.asyncio
async def test_notify_both_delivers_on_slack_and_names_the_telegram_root_chat(playbook_dir):
    service = fake_service(slack=fake_slack(owner("slack", "telegram")))
    resp = await fire(service, {"channel": "notify"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["channel"] == "slack" and data["mirror"] == ["telegram"]
    assert data["resolution"] == "argus.owner: slack, telegram"
    msg = published(service)
    assert (msg.channel_name, msg.chat_id) == ("slack", DM)
    # Root rows only (#52): the schedule's own Telegram topic row is not a mirror target.
    assert msg.metadata[MIRROR_KEY] == {"channel": "telegram", "chat_ids": [TELEGRAM_CHAT]}


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [{"telegram_rows": []}, {"telegram": False}])
async def test_notify_both_without_a_telegram_chat_is_not_an_error(playbook_dir, state):
    service = fake_service(slack=fake_slack(owner("slack", "telegram")), **state)
    resp = await fire(service, {"channel": "notify"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["channel"] == "slack" and resp.json()["mirror"] == []
    assert MIRROR_KEY not in published(service).metadata


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("slack", "resolution"),
    [
        (fake_slack(None, "router_unreachable: ConnectionError"), "argus.owner failed (router_unreachable: ConnectionError); fell back to telegram"),
        (fake_slack(None, "method_not_allowed"), "argus.owner failed (method_not_allowed); fell back to telegram"),
        (fake_slack(owner()), "argus.owner named no channel; fell back to telegram"),
        (None, "slack is not running on the relay transport; telegram"),
    ],
)
async def test_notify_falls_back_to_telegram_without_a_router_answer(playbook_dir, slack, resolution):
    service = fake_service(slack=slack)
    resp = await fire(service, {"channel": "notify"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["channel"] == "telegram" and resp.json()["resolution"] == resolution
    assert published(service).channel_name == "telegram"


@pytest.mark.asyncio
async def test_notify_on_slack_without_relay_falls_back_to_telegram(playbook_dir):
    slack = fake_slack(owner("slack"))
    slack.uses_relay_transport = False
    service = fake_service(slack=slack)
    resp = await fire(service, {"channel": "notify"})
    assert resp.status_code == 200
    assert resp.json()["channel"] == "telegram"
    slack.owner_conversation.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("slack", "reason"),
    [
        (fake_slack(owner("telegram")), "argus.owner: telegram"),
        (fake_slack(None, "router_unreachable: timed out"), "argus.owner failed (router_unreachable: timed out)"),
    ],
)
async def test_notify_telegram_without_a_telegram_channel_is_409(playbook_dir, slack, reason):
    service = fake_service(slack=slack, telegram=False)
    resp = await fire(service, {"channel": "notify"})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "no telegram channel is running on this stack" in detail and reason in detail
    service.bus.publish_inbound.assert_not_called()


@pytest.mark.asyncio
async def test_notify_slack_without_an_owner_dm_is_409_like_channel_slack(playbook_dir):
    service = fake_service(slack=fake_slack(owner("slack", dm="")))
    resp = await fire(service, {"channel": "notify"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "no Slack DM for this stack's owner: the router has no DM for the owner"
    service.bus.publish_inbound.assert_not_called()


@pytest.mark.asyncio
async def test_an_explicit_channel_never_asks_the_router(playbook_dir):
    slack = fake_slack(owner("slack"))
    service = fake_service(slack=slack)
    resp = await fire(service, {"channel": "telegram"})
    assert resp.status_code == 200
    assert resp.json()["channel"] == "telegram" and resp.json()["resolution"] is None
    slack.owner_conversation.assert_not_called()


# -- the mirror ------------------------------------------------------------------------


def _inbound(metadata: dict) -> InboundMessage:
    return InboundMessage(channel_name="slack", chat_id=DM, user_id=OWNER, text="go", metadata=metadata)


def test_mirror_outbounds_are_text_only_copies_without_a_thread():
    (copy,) = mirror_outbounds(_inbound({MIRROR_KEY: {"channel": "telegram", "chat_ids": [TELEGRAM_CHAT, TELEGRAM_CHAT, "", 7]}}), "thread-1", "Two meetings today.")
    assert (copy.channel_name, copy.chat_id, copy.thread_id, copy.text) == ("telegram", TELEGRAM_CHAT, "thread-1", "Two meetings today.")
    assert copy.thread_ts is None and copy.attachments == [] and copy.is_final is True
    assert copy.metadata == {"argus_mirror_of": "slack"}


@pytest.mark.parametrize(
    "metadata",
    [{}, {MIRROR_KEY: "telegram"}, {MIRROR_KEY: {"channel": "slack", "chat_ids": [DM]}}, {MIRROR_KEY: {"channel": "telegram", "chat_ids": TELEGRAM_CHAT}}, {MIRROR_KEY: {"chat_ids": [TELEGRAM_CHAT]}}],
)
def test_mirror_outbounds_ignore_anything_else(metadata):
    assert mirror_outbounds(_inbound(metadata), "thread-1", "text") == []


class RouterClient:
    def __init__(self, **kwargs):
        self.token = kwargs.get("token")
        self.calls: list[tuple[str, dict]] = []
        self.owner = {"ok": True, "enabled": True, "user_id": OWNER, "dm": DM, "channels": ["slack", "telegram"]}
        self.post_error: BaseException | None = None

    def auth_test(self, **kwargs):
        return {"ok": True, "user_id": "UATLAS"}

    def api_call(self, method, **kwargs):
        self.calls.append((method, kwargs))
        return self.owner

    def chat_postMessage(self, **kwargs):
        self.calls.append(("chat.postMessage", kwargs))
        if self.post_error is not None:
            raise self.post_error
        return {"ok": True, "ts": "1730000000.000001"}


def _langgraph_client(answer: str) -> MagicMock:
    client = MagicMock()
    client.threads.create = AsyncMock(return_value={"thread_id": "thread-notify"})
    client.threads.update = AsyncMock(return_value={"thread_id": "thread-notify"})
    client.threads.get = AsyncMock(return_value={"thread_id": "thread-notify", "metadata": {}})
    client.runs.wait = AsyncMock(return_value={"messages": [{"type": "human", "content": "go"}, {"type": "ai", "content": answer}]})
    return client


async def _until(condition, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.02)


async def _run_notify(tmp_path: Path, *, answer: str, post_error: BaseException | None = None):
    """Fire ``channel: notify`` through a real manager, relay channel and JSON store."""
    from app.channels.manager import ChannelManager

    store = JsonChannelStore(path=tmp_path / "store.json")
    await store.set_thread_id("telegram", TELEGRAM_CHAT, "thread-telegram-main", user_id=TELEGRAM_CHAT)
    bus = MessageBus()
    channel = SlackChannel(bus=bus, config={"transport": "relay", "relay_url": "http://slack-router:8080/api", "relay_token": "k", "web_client_factory": RouterClient, "channel_store": store})
    await channel.start()
    channel._web_client.post_error = post_error
    channel.send = functools.partial(SlackChannel.send, channel, _max_retries=1)
    telegram_out = []

    async def telegram(msg):
        if msg.channel_name == "telegram":
            telegram_out.append(msg)

    bus.subscribe_outbound(telegram)
    service = MagicMock()
    service.get_channel.side_effect = {"slack": channel, "telegram": MagicMock(supports_streaming=True)}.get
    service.store = store
    service.bus = bus
    manager = ChannelManager(bus=bus, store=store)
    manager._client = _langgraph_client(answer)
    report = AsyncMock(return_value=True)
    try:
        with patch("app.channels.service.get_channel_service", return_value=service), patch("app.channels.manager.report_delivery", report):
            await manager.start()
            resp = await fire(service, {"channel": "notify", "topic_id": f"sched:{SCHEDULE_ID}", "report_url": REPORT_URL})
            assert resp.status_code == 200, resp.text
            await _until(lambda: report.await_count == 1)
    finally:
        await manager.stop()
        await channel.stop()
    return resp.json(), channel._web_client, telegram_out, report.await_args.kwargs, store


@pytest.mark.asyncio
async def test_notify_both_end_to_end_mirrors_the_final_text_to_telegram(playbook_dir, tmp_path):
    data, client, telegram_out, report, store = await _run_notify(tmp_path, answer="Two meetings today.")
    assert data["channel"] == "slack" and data["mirror"] == ["telegram"]
    (post,) = [kw for name, kw in client.calls if name == "chat.postMessage"]
    assert post["channel"] == DM and "thread_ts" not in post
    (mirror,) = telegram_out
    assert (mirror.chat_id, mirror.text, mirror.thread_ts, mirror.thread_id) == (TELEGRAM_CHAT, "Two meetings today.", None, "thread-notify")
    # The report names the channel the run went to, Slack, and the owner's DM.
    assert report["status"] == "delivered" and report["channel"] == "slack" and report["chat_id"] == DM
    # The mirror binds nothing: the Telegram main chat keeps its own thread.
    assert await store.get_thread_id("telegram", TELEGRAM_CHAT) == "thread-telegram-main"
    assert await store.get_thread_id("slack", DM, topic_id="1730000000.000001") == "thread-notify"


@pytest.mark.asyncio
async def test_a_silent_notify_turn_mirrors_nothing(playbook_dir, tmp_path):
    _, client, telegram_out, report, _ = await _run_notify(tmp_path, answer="")
    assert report["status"] == "silent"
    assert telegram_out == []
    assert [name for name, _ in client.calls if name == "chat.postMessage"] == []


@pytest.mark.asyncio
async def test_a_refused_slack_post_still_mirrors_and_reports_failed(playbook_dir, tmp_path):
    refused = SlackApiError("refused", {"ok": False, "error": "stack_not_enabled"})
    _, _, telegram_out, report, _ = await _run_notify(tmp_path, answer="Two meetings today.", post_error=refused)
    assert [m.text for m in telegram_out] == ["Two meetings today."]
    assert report["status"] == "failed" and report["error"] == "slack post failed: stack_not_enabled"
    assert report["channel"] == "slack"

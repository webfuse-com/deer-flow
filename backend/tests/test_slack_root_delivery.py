"""[argus patch #172] A playbook fire with ``channel: slack`` lands in the owner's DM as its own thread.

On the relay transport (#171) the gateway asks the Slack router for the
stack owner's DM (``argus.owner``), publishes the run there, and the Slack
adapter posts an outbound without ``thread_ts`` as a new top-level DM message,
then binds ``(slack, DM, root_ts)`` to the run's DeerFlow thread so a reply
under it continues that run. Synthetic topics are not hydrated from Slack, and
a refused post reports ``failed`` to Chronos instead of ``delivered``.
"""

from __future__ import annotations

import asyncio
import functools
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from slack_sdk.errors import SlackApiError

import app.gateway.internal_auth as internal_auth
import app.gateway.routers.playbooks as playbooks
from app.channels._delivery_outcome import DELIVERY_ERROR_KEY
from app.channels.message_bus import MessageBus, OutboundMessage, ResolvedAttachment
from app.channels.slack import SlackChannel, SlackOwner
from app.channels.store import JsonChannelStore

RELAY_URL = "http://slack-router:8080/api"
RELAY_TOKEN = "router-key-for-atlas-nicholas"
DM = "D-owner"
OWNER = "U-owner"
TOKEN_HEADER = internal_auth.INTERNAL_AUTH_HEADER_NAME
TEST_TOKEN = "test-internal-token"
SCHEDULE_ID = "meeting-prep"
REPORT_URL = "http://argus-scheduler:8000/api/runs/7/output"


class RouterClient:
    """A relay WebClient double: answers ``argus.owner`` and numbers its posts like Slack."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.token = kwargs.get("token")
        self.calls: list[tuple[str, dict]] = []
        self.owner: dict | BaseException = {"ok": True, "enabled": True, "user_id": OWNER, "dm": DM, "channels": ["slack"]}
        self.post_error: BaseException | None = None
        self._posts = 0

    def auth_test(self, **kwargs):
        self.calls.append(("auth.test", kwargs))
        return {"ok": True, "user_id": "UATLAS"}

    def api_call(self, method, **kwargs):
        self.calls.append((method, kwargs))
        if isinstance(self.owner, BaseException):
            raise self.owner
        return self.owner

    def chat_postMessage(self, **kwargs):
        self.calls.append(("chat.postMessage", kwargs))
        if self.post_error is not None:
            raise self.post_error
        self._posts += 1
        return {"ok": True, "ts": f"1730000000.{self._posts:06d}"}

    def reactions_add(self, **kwargs):
        self.calls.append(("reactions.add", kwargs))
        return {"ok": True}

    def reactions_remove(self, **kwargs):
        self.calls.append(("reactions.remove", kwargs))
        return {"ok": True}

    def chat_delete(self, **kwargs):
        self.calls.append(("chat.delete", kwargs))
        return {"ok": True}

    def files_upload_v2(self, **kwargs):
        self.calls.append(("files.upload_v2", kwargs))
        return {"ok": True}

    def conversations_replies(self, **kwargs):
        self.calls.append(("conversations.replies", kwargs))
        return {"ok": True, "messages": [{"ts": kwargs.get("ts"), "user": OWNER, "text": "earlier"}]}

    def named(self, method: str) -> list[dict]:
        return [kwargs for name, kwargs in self.calls if name == method]


def relay_config(store=None, **overrides) -> dict:
    config = {"transport": "relay", "relay_url": RELAY_URL, "relay_token": RELAY_TOKEN, "tier_agents": {"dm": "atlas"}, "web_client_factory": RouterClient}
    if store is not None:
        config["channel_store"] = store
    config.update(overrides)
    return config


async def started(config=None, bus=None) -> SlackChannel:
    channel = SlackChannel(bus=bus or MessageBus(), config=config or relay_config())
    await channel.start()
    return channel


@pytest.fixture()
def store(tmp_path: Path) -> JsonChannelStore:
    return JsonChannelStore(path=tmp_path / "store.json")


def outbound(**overrides) -> OutboundMessage:
    fields = {"channel_name": "slack", "chat_id": DM, "thread_id": "thread-run-1", "text": "Your meetings today", "metadata": {"user_id": OWNER}}
    fields.update(overrides)
    return OutboundMessage(**fields)


# -- argus.owner ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_owner_conversation_asks_the_router_once():
    channel = await started()
    try:
        channel._web_client.owner = {"ok": True, "enabled": True, "user_id": OWNER, "dm": DM, "channels": ["slack", "email", "telegram", "slack"]}
        owner, error = await channel.owner_conversation()
    finally:
        await channel.stop()
    assert error == ""
    # Unknown channels are dropped, duplicates collapse, the order is kept.
    assert owner == SlackOwner(enabled=True, user_id=OWNER, dm=DM, channels=("slack", "telegram"))
    assert channel._web_client.named("argus.owner") == [{}]


@pytest.mark.asyncio
async def test_owner_conversation_reads_a_disabled_stack():
    channel = await started()
    try:
        channel._web_client.owner = {"ok": True, "enabled": False, "user_id": "", "dm": "", "channels": ["telegram"]}
        owner, error = await channel.owner_conversation()
    finally:
        await channel.stop()
    assert error == "" and owner == SlackOwner(enabled=False, user_id="", dm="", channels=("telegram",))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (SlackApiError("refused", {"ok": False, "error": "method_not_allowed"}), "method_not_allowed"),
        (ConnectionError("router down"), "router_unreachable: ConnectionError"),
        (SlackApiError("HTTP 502", {}), "router_unreachable: SlackApiError"),
        ({"ok": "yes"}, "malformed argus.owner answer"),
    ],
)
async def test_owner_conversation_failures(answer, expected):
    channel = await started()
    try:
        channel._web_client.owner = answer
        assert await channel.owner_conversation() == (None, expected)
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_owner_conversation_gives_up_on_a_silent_router(monkeypatch):
    import app.channels.slack as slack_module

    monkeypatch.setattr(slack_module, "_SLACK_ARGUS_OWNER_TIMEOUT_SECONDS", 0.05)
    channel = await started()
    try:
        channel._web_client.api_call = lambda method, **kwargs: time.sleep(0.5)
        owner, error = await channel.owner_conversation()
    finally:
        await channel.stop()
    assert owner is None and error.startswith("router_unreachable: no answer")


@pytest.mark.asyncio
async def test_owner_conversation_needs_the_relay_transport():
    channel = SlackChannel(bus=MessageBus(), config={"bot_token": "xoxb", "app_token": "xapp"})
    assert await channel.owner_conversation() == (None, "slack is not running on the relay transport")


# -- the root post and its binding ------------------------------------------------


@pytest.mark.asyncio
async def test_an_outbound_without_thread_ts_posts_a_root_and_binds_it(store):
    channel = await started(relay_config(store))
    msg = outbound()
    try:
        await channel.send(msg)
    finally:
        await channel.stop()
    client = channel._web_client
    (post,) = client.named("chat.postMessage")
    assert post["channel"] == DM and "thread_ts" not in post
    root_ts = "1730000000.000001"
    assert await store.get_thread_id("slack", DM, topic_id=root_ts) == "thread-run-1"
    (row,) = [e for e in await store.list_entries("slack") if e.get("topic_id") == root_ts]
    assert row["user_id"] == OWNER
    # Atlas's own root gets no check mark; later sends for this message go under it.
    assert client.named("reactions.add") == []
    assert msg.thread_ts == root_ts
    assert DELIVERY_ERROR_KEY not in msg.metadata


@pytest.mark.asyncio
async def test_attachments_follow_into_the_roots_thread(store, tmp_path):
    channel = await started(relay_config(store))
    report = tmp_path / "brief.pdf"
    report.write_bytes(b"%PDF")
    attachment = ResolvedAttachment(virtual_path="/mnt/user-data/outputs/brief.pdf", actual_path=report, filename="brief.pdf", mime_type="application/pdf", size=4, is_image=False)
    try:
        await channel._on_outbound(outbound(attachments=[attachment]))
    finally:
        await channel.stop()
    (upload,) = channel._web_client.named("files.upload_v2")
    assert upload["channel"] == DM and upload["thread_ts"] == "1730000000.000001"


@pytest.mark.asyncio
async def test_a_failing_store_write_neither_raises_nor_reposts(store):
    store.set_thread_id = AsyncMock(side_effect=RuntimeError("disk full"))
    channel = await started(relay_config(store))
    msg = outbound()
    try:
        await channel.send(msg)
    finally:
        await channel.stop()
    assert len(channel._web_client.named("chat.postMessage")) == 1
    store.set_thread_id.assert_awaited_once()
    assert msg.thread_ts == "1730000000.000001"


@pytest.mark.asyncio
async def test_a_threaded_outbound_binds_nothing(store):
    channel = await started(relay_config(store))
    try:
        await channel.send(outbound(thread_ts="1710000000.000100"))
    finally:
        await channel.stop()
    client = channel._web_client
    assert client.named("chat.postMessage")[0]["thread_ts"] == "1710000000.000100"
    assert [r["name"] for r in client.named("reactions.add")] == ["white_check_mark"]
    assert await store.list_entries("slack") == []


@pytest.mark.asyncio
async def test_an_outbound_without_a_thread_posts_but_binds_nothing(store):
    channel = await started(relay_config(store))
    try:
        await channel.send(outbound(thread_id=""))
    finally:
        await channel.stop()
    assert len(channel._web_client.named("chat.postMessage")) == 1
    assert await store.list_entries("slack") == []


@pytest.mark.asyncio
async def test_socket_mode_keeps_upstream_behaviour(store, monkeypatch):
    import slack_sdk.socket_mode

    class Socket:
        def __init__(self, **kwargs):
            self.socket_mode_request_listeners = []

        def connect(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(slack_sdk.socket_mode, "SocketModeClient", Socket)
    channel = await started({"bot_token": "xoxb", "app_token": "xapp", "bot_user_id": "UBOT", "web_client_factory": RouterClient, "channel_store": store})
    msg = outbound(chat_id="C1")
    try:
        await channel.send(msg)
    finally:
        await channel.stop()
    assert await store.list_entries("slack") == []
    assert msg.thread_ts is None


@pytest.mark.asyncio
async def test_a_refused_post_records_why_for_the_delivery_report(store):
    channel = await started(relay_config(store))
    channel._web_client.post_error = SlackApiError("refused", {"ok": False, "error": "stack_not_enabled"})
    channel.send = functools.partial(SlackChannel.send, channel, _max_retries=1)
    msg = outbound()
    try:
        # _on_outbound swallows the send error, as it always has.
        await channel._on_outbound(msg)
    finally:
        await channel.stop()
    assert msg.metadata[DELIVERY_ERROR_KEY] == "stack_not_enabled"
    assert await store.list_entries("slack") == []


@pytest.mark.asyncio
async def test_a_send_without_a_web_client_records_why():
    channel = SlackChannel(bus=MessageBus(), config=relay_config())
    msg = outbound()
    await channel.send(msg)
    assert msg.metadata[DELIVERY_ERROR_KEY] == "no Slack web client"


# -- #22 hydration ---------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("topic", ["sched:meeting-prep", "hook:order-intake:new-order", "notify:minutes:42", "1730000000", "abc.def"])
async def test_synthetic_topics_are_not_hydrated(topic):
    channel = await started()
    try:
        assert await channel.fetch_thread_context(DM, topic) == ""
    finally:
        await channel.stop()
    assert channel._web_client.named("conversations.replies") == []


@pytest.mark.asyncio
async def test_a_real_thread_is_still_hydrated():
    channel = await started()
    try:
        context = await channel.fetch_thread_context(DM, "1710000000.000100")
    finally:
        await channel.stop()
    assert "earlier" in context
    assert channel._web_client.named("conversations.replies")[0]["ts"] == "1710000000.000100"


# -- the fire route ----------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _pin_internal_token():
    with patch.object(internal_auth, "_INTERNAL_AUTH_TOKEN", TEST_TOKEN):
        yield


@pytest.fixture()
def playbook_dir(tmp_path: Path):
    (tmp_path / f"{SCHEDULE_ID}.md").write_text("Prepare my meetings for {{TODAY}}.", encoding="utf-8")
    with patch.object(playbooks, "PLAYBOOK_DIR", tmp_path):
        yield tmp_path


def fake_slack(owner: SlackOwner | None = None, error: str = "", *, relay: bool = True) -> MagicMock:
    slack = MagicMock()
    slack.uses_relay_transport = relay
    slack.owner_conversation = AsyncMock(return_value=(owner, error))
    return slack


def fake_service(channels: dict, entries: list[dict] | None = None) -> MagicMock:
    service = MagicMock()
    service.get_channel.side_effect = channels.get
    service.store.list_entries = AsyncMock(return_value=entries or [])
    service.store.remove = AsyncMock(return_value=True)
    service.bus.publish_inbound = AsyncMock()
    return service


async def fire(service, body: dict, *, schedule_id: str = SCHEDULE_ID) -> httpx.Response:
    app = FastAPI()
    app.include_router(playbooks.router)
    with patch("app.channels.service.get_channel_service", return_value=service):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as http:
            return await http.post(f"/api/playbooks/{schedule_id}/fire", json=body, headers={TOKEN_HEADER: TEST_TOKEN})


READY = SlackOwner(enabled=True, user_id=OWNER, dm=DM, channels=("slack",))


@pytest.mark.asyncio
async def test_fire_slack_publishes_into_the_owners_dm(playbook_dir):
    service = fake_service({"slack": fake_slack(READY)})
    resp = await fire(service, {"channel": "slack", "topic_id": f"sched:{SCHEDULE_ID}", "report_url": REPORT_URL, "trigger": "scheduled"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"accepted": True, "schedule_id": SCHEDULE_ID, "channel": "slack", "agent": "atlas", "memory": "read-only", "chats": 1, "resolution": None, "mirror": []}
    msg = service.bus.publish_inbound.await_args.args[0]
    assert (msg.channel_name, msg.chat_id, msg.user_id) == ("slack", DM, OWNER)
    assert msg.topic_id == f"sched:{SCHEDULE_ID}" and msg.thread_ts is None
    assert msg.unattended is True and msg.report_url == REPORT_URL
    assert msg.metadata["tier"] == "dm" and msg.metadata["user_id"] == OWNER
    assert msg.metadata["source"] == "playbook" and msg.metadata["schedule_id"] == SCHEDULE_ID
    # The Slack store has no root rows to look up; the router named the DM.
    service.store.list_entries.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owner", "error", "status", "reason"),
    [
        (SlackOwner(enabled=False, user_id="", dm="", channels=("telegram",)), "", 409, "Slack is not enabled for this stack"),
        (SlackOwner(enabled=True, user_id=OWNER, dm="", channels=("slack",)), "", 409, "the router has no DM for the owner"),
        (None, "method_not_allowed", 409, "argus.owner failed (method_not_allowed)"),
        (None, "router_unreachable: ConnectionError", 503, "argus.owner failed (router_unreachable: ConnectionError)"),
    ],
)
async def test_fire_slack_without_an_owner_dm_says_why(playbook_dir, owner, error, status, reason):
    service = fake_service({"slack": fake_slack(owner, error)})
    resp = await fire(service, {"channel": "slack"})
    assert resp.status_code == status
    assert resp.json()["detail"].startswith("no Slack DM for this stack's owner") and reason in resp.json()["detail"]
    service.bus.publish_inbound.assert_not_called()


@pytest.mark.asyncio
async def test_fire_slack_without_relay_keeps_the_root_row_lookup(playbook_dir):
    slack = fake_slack(READY, relay=False)
    service = fake_service({"slack": slack}, entries=[{"channel_name": "slack", "chat_id": DM, "user_id": OWNER, "topic_id": "1710000000.000100"}])
    resp = await fire(service, {"channel": "slack"})
    assert resp.status_code == 409
    slack.owner_conversation.assert_not_called()


@pytest.mark.asyncio
async def test_fire_slack_history_none_drops_only_the_schedule_topic(playbook_dir):
    service = fake_service({"slack": fake_slack(READY)})
    resp = await fire(service, {"channel": "slack", "topic_id": f"sched:{SCHEDULE_ID}", "history": "none"})
    assert resp.status_code == 200
    service.store.remove.assert_awaited_once_with("slack", DM, f"sched:{SCHEDULE_ID}")


# -- end to end: fire, root post, reply in the same thread ---------------------------------


def _langgraph_client(thread_id: str) -> MagicMock:
    client = MagicMock()
    client.threads.create = AsyncMock(return_value={"thread_id": thread_id})
    client.threads.update = AsyncMock(return_value={"thread_id": thread_id})
    client.threads.get = AsyncMock(return_value={"thread_id": thread_id, "metadata": {}})
    client.runs.wait = AsyncMock(return_value={"messages": [{"type": "human", "content": "go"}, {"type": "ai", "content": "Two meetings today."}]})
    return client


async def _until(condition, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_fire_then_reply_under_the_root_continues_the_runs_thread(playbook_dir, store):
    from app.channels.manager import ChannelManager

    bus = MessageBus()
    channel = await started(relay_config(store), bus=bus)
    service = MagicMock()
    service.get_channel.side_effect = {"slack": channel}.get
    service.store = store
    service.bus = bus
    manager = ChannelManager(bus=bus, store=store)
    manager._client = _langgraph_client("thread-run-1")
    client = channel._web_client
    report = AsyncMock(return_value=True)
    try:
        with patch("app.channels.service.get_channel_service", return_value=service), patch("app.channels.manager.report_delivery", report):
            await manager.start()
            resp = await fire(service, {"channel": "slack", "topic_id": f"sched:{SCHEDULE_ID}", "history": "none", "report_url": REPORT_URL})
            assert resp.status_code == 200, resp.text
            await _until(lambda: report.await_count == 1)

            (root_post,) = client.named("chat.postMessage")
            assert root_post["channel"] == DM and "thread_ts" not in root_post
            root_ts = "1730000000.000001"
            assert await store.get_thread_id("slack", DM, topic_id=root_ts) == "thread-run-1"
            # The schedule's synthetic topic was never sent to Slack as a thread.
            assert client.named("conversations.replies") == []
            assert report.await_args.kwargs["status"] == "delivered"
            assert report.await_args.kwargs["channel"] == "slack" and report.await_args.kwargs["chat_id"] == DM
            assert report.await_args.kwargs["thread_id"] == "thread-run-1"

            # The owner replies under the run's root message.
            reply = {"type": "message", "channel_type": "im", "channel": DM, "user": OWNER, "text": "move the second one", "ts": "1730000100.000100", "thread_ts": root_ts}
            accepted = await channel.relay_event({"type": "events_api", "team_id": "T1", "event": reply}, {"tier": "dm", "trigger_user": OWNER, "action": "message"})
            assert accepted is True
            await _until(lambda: manager._client.runs.wait.await_count == 2)
            await _until(lambda: len([p for p in client.named("chat.postMessage") if p.get("thread_ts") == root_ts and "Two meetings" in p.get("text", "")]) == 1)
    finally:
        await manager.stop()
        await channel.stop()

    lg = manager._client
    lg.threads.create.assert_awaited_once()
    assert [call.args[0] for call in lg.runs.wait.await_args_list] == ["thread-run-1", "thread-run-1"]
    # The reply's answer went into the root's thread and closed its acks there.
    assert {"channel": DM, "timestamp": root_ts, "name": "white_check_mark"} in client.named("reactions.add")


@pytest.mark.asyncio
async def test_a_refused_final_post_reports_failed(playbook_dir, store):
    from app.channels.manager import ChannelManager

    bus = MessageBus()
    channel = await started(relay_config(store), bus=bus)
    channel._web_client.post_error = SlackApiError("refused", {"ok": False, "error": "stack_not_enabled"})
    channel.send = functools.partial(SlackChannel.send, channel, _max_retries=1)
    service = MagicMock()
    service.get_channel.side_effect = {"slack": channel}.get
    service.store = store
    service.bus = bus
    manager = ChannelManager(bus=bus, store=store)
    manager._client = _langgraph_client("thread-run-2")
    report = AsyncMock(return_value=True)
    try:
        with patch("app.channels.service.get_channel_service", return_value=service), patch("app.channels.manager.report_delivery", report):
            await manager.start()
            resp = await fire(service, {"channel": "slack", "report_url": REPORT_URL})
            assert resp.status_code == 200
            await _until(lambda: report.await_count == 1)
    finally:
        await manager.stop()
        await channel.stop()
    kwargs = report.await_args.kwargs
    assert kwargs["status"] == "failed"
    assert kwargs["error"] == "slack post failed: stack_not_enabled"
    assert kwargs["channel"] == "slack" and kwargs["chat_id"] == DM
    # Only the run's own binding (no topic): nothing was posted, so no root alias.
    assert all(not e.get("topic_id") for e in await store.list_entries("slack"))

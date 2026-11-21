"""[argus patch #171] Slack relay transport for a router-held Slack app.

A router (acropolis ``argus-slack-router``) holds the only Slack tokens and
the Socket Mode connection. The gateway's Slack channel runs with
``transport: relay``: no Socket Mode client, every WebClient aimed at the
router's ``relay_url``, and events delivered to
``POST /api/channels/slack/relay`` (internal token) feeding the same handler
a Socket Mode event takes.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.gateway.internal_auth as internal_auth
from app.channels.message_bus import InboundMessage, InboundMessageType, MessageBus
from app.channels.service import _channel_has_credentials, channel_owner_emails_from_config
from app.channels.slack import SlackChannel
from app.gateway.routers.channels import router

RELAY_URL = "http://slack-router:8080/api"
RELAY_TOKEN = "router-key-for-atlas-nicholas"


class FakeWebClient:
    """Records how it was built and every Web API call made through it."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.token = kwargs.get("token")
        self.calls: list[tuple[str, dict]] = []

    def auth_test(self, **kwargs):
        self.calls.append(("auth.test", kwargs))
        # The router's synthetic auth.test answer.
        return {"ok": True, "user_id": "UATLAS", "bot_id": "BATLAS", "team_id": "T1", "user": "atlas"}

    def reactions_add(self, **kwargs):
        self.calls.append(("reactions.add", kwargs))
        return {"ok": True}

    def chat_postMessage(self, **kwargs):
        self.calls.append(("chat.postMessage", kwargs))
        return {"ok": True, "ts": "ack.1"}


class Factory:
    def __init__(self):
        self.clients: list[FakeWebClient] = []

    def __call__(self, **kwargs):
        client = FakeWebClient(**kwargs)
        self.clients.append(client)
        return client


def relay_config(**overrides):
    config = {
        "transport": "relay",
        "relay_url": RELAY_URL,
        "relay_token": RELAY_TOKEN,
        "tier_agents": {"dm": "atlas", "public": "atlas-public"},
        "web_client_factory": Factory(),
    }
    config.update(overrides)
    return config


@pytest.fixture()
def no_socket_mode(monkeypatch):
    """Fail the test if anything builds a Socket Mode client."""
    import slack_sdk.socket_mode

    built = []

    class Refused:
        def __init__(self, **kwargs):
            built.append(kwargs)
            raise AssertionError("relay transport must not build a Socket Mode client")

    monkeypatch.setattr(slack_sdk.socket_mode, "SocketModeClient", Refused)
    return built


def envelope(event: dict, **extra) -> dict:
    return {"type": "events_api", "team_id": "T1", "event_id": "Ev1", "event": event, **extra}


def dm_event(**overrides) -> dict:
    event = {"type": "message", "channel_type": "im", "channel": "D1", "user": "U-owner", "text": "what is on my calendar", "ts": "1710000000.000100"}
    event.update(overrides)
    return event


def argus(tier="dm", **overrides) -> dict:
    block = {"tier": tier, "trigger_user": "U-owner", "owner_user_id": "U-owner", "persona": "Atlas for Nicholas", "action": "message"}
    block.update(overrides)
    return block


@pytest.fixture()
def public_tier_policy(tmp_path, monkeypatch):
    """[argus patch #174] A restricted tier needs the image's tier policy and its agent."""
    import json

    from deerflow.agents import tier_policy

    path = tmp_path / "argus-tier-policy.json"
    path.write_text(json.dumps({"version": 1, "tiers": {"public": {"agent": "atlas-slack-public", "tools": []}}}))
    monkeypatch.setattr(tier_policy, "TIER_POLICY_PATH", path)
    monkeypatch.setattr(tier_policy, "tier_agent_present", lambda name, *, user_id=None: True)
    tier_policy.reset_tier_policy_cache()
    yield
    tier_policy.reset_tier_policy_cache()


async def started(config=None) -> tuple[SlackChannel, MessageBus]:
    bus = MessageBus()
    channel = SlackChannel(bus=bus, config=config or relay_config())
    await channel.start()
    return channel, bus


# -- start / stop -------------------------------------------------------------


@pytest.mark.asyncio
async def test_relay_start_builds_no_socket_client_and_aims_the_web_client_at_the_router(no_socket_mode):
    config = relay_config()
    channel, bus = await started(config)
    try:
        assert channel.is_running
        assert channel.uses_relay_transport
        assert channel._socket_client is None
        assert channel._socket_connect_future is None
        assert no_socket_mode == []
        (client,) = config["web_client_factory"].clients
        # slack_sdk urljoins the method onto base_url: the trailing slash is load-bearing.
        assert client.kwargs == {"token": RELAY_TOKEN, "base_url": "http://slack-router:8080/api/"}
        # _initialize_operator_web_client still runs: the bot user id comes from the router's auth.test.
        assert ("auth.test", {}) in client.calls
        assert channel._bot_user_id == "UATLAS"
        assert channel._on_outbound in bus._outbound_listeners
    finally:
        await channel.stop()
    assert not channel.is_running
    assert channel._on_outbound not in bus._outbound_listeners


@pytest.mark.asyncio
async def test_relay_start_ignores_slack_tokens_and_needs_relay_url_and_token(no_socket_mode, caplog):
    channel, _ = await started(relay_config(relay_token="", bot_token="xoxb-real", app_token="xapp-real"))
    assert not channel.is_running
    channel, _ = await started(relay_config(relay_url="slack-router:8080/api"))  # not an http(s) URL
    assert not channel.is_running
    assert "requires relay_url" in caplog.text
    assert no_socket_mode == []


@pytest.mark.asyncio
async def test_relay_stop_works_without_a_socket_client(no_socket_mode):
    channel, _ = await started()
    await channel.stop()
    await channel.stop()  # idempotent
    assert channel._socket_close_task is None


@pytest.mark.asyncio
async def test_per_connection_web_clients_use_the_router_too():
    config = relay_config()
    repo = MagicMock()
    repo.get_credentials = AsyncMock(return_value={"access_token": "conn-token"})
    config["connection_repo"] = repo
    channel, _ = await started(config)
    try:
        client = await channel._web_client_for_connection("conn-1")
        assert client.kwargs == {"token": "conn-token", "base_url": "http://slack-router:8080/api/"}
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_socket_mode_web_clients_get_no_base_url(monkeypatch):
    import slack_sdk.socket_mode

    sockets = []

    class Socket:
        def __init__(self, **kwargs):
            self.socket_mode_request_listeners = []
            sockets.append(self)

        def connect(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(slack_sdk.socket_mode, "SocketModeClient", Socket)
    factory = Factory()
    channel = SlackChannel(MessageBus(), {"bot_token": "xoxb", "app_token": "xapp", "bot_user_id": "UBOT", "web_client_factory": factory, "relay_url": RELAY_URL})
    await channel.start()
    try:
        assert not channel.uses_relay_transport
        assert factory.clients[0].kwargs == {"token": "xoxb"}
        assert len(sockets) == 1
    finally:
        await channel.stop()


# -- relay_event ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_relay_event_publishes_inbound_with_tier_metadata_and_tier_agent():
    channel, bus = await started()
    try:
        assert await channel.relay_event(envelope(dm_event()), argus("dm")) is True
        inbound = await asyncio.wait_for(bus.get_inbound(), 2)
    finally:
        await channel.stop()
    assert isinstance(inbound, InboundMessage)
    assert inbound.channel_name == "slack"
    assert inbound.chat_id == "D1"
    assert inbound.user_id == "U-owner"
    assert inbound.text == "what is on my calendar"
    assert inbound.topic_id == "1710000000.000100"
    assert inbound.agent_name == "atlas"
    assert inbound.metadata["tier"] == "dm"
    assert inbound.metadata["trigger_user"] == "U-owner"
    assert inbound.metadata["persona"] == "Atlas for Nicholas"
    assert inbound.metadata["team_id"] == "T1"
    assert inbound.metadata["event_ts"] == "1710000000.000100"
    # owner_user_id in the argus block is a Slack id; it never becomes the web owner.
    assert inbound.owner_user_id is None
    # Same path as Socket Mode: eyes reaction and progress reply go through the router client.
    calls = [name for name, _ in channel._web_client.calls]
    assert "reactions.add" in calls and "chat.postMessage" in calls


@pytest.mark.asyncio
async def test_relay_event_picks_the_agent_per_tier(public_tier_policy):
    channel, bus = await started()
    try:
        event = {"type": "app_mention", "channel": "C1", "user": "U-staff", "text": "<@UATLAS> summarise this thread", "ts": "2.0", "thread_ts": "1.0"}
        assert await channel.relay_event(envelope(event), argus("public", trigger_user="U-staff")) is True
        inbound = await asyncio.wait_for(bus.get_inbound(), 2)
        # [argus patch #174] private has no tier policy here: refused, never the channel default.
        assert await channel.relay_event(envelope(dict(event, ts="3.0")), argus("private", trigger_user="U-staff")) is False
    finally:
        await channel.stop()
    # [argus patch #174] A restricted tier runs the tier policy's agent;
    # tier_agents.public ("atlas-public") is only the stack's opt-in.
    assert inbound.agent_name == "atlas-slack-public"
    assert inbound.metadata["tier"] == "public"
    assert inbound.metadata["argus_tier"] == "public"
    assert inbound.metadata["trigger_user"] == "U-staff"
    assert inbound.text == "summarise this thread"  # the bot mention from the router's auth.test is stripped
    assert inbound.topic_id == "1.0"


@pytest.mark.asyncio
async def test_relay_skips_allowed_users_but_socket_events_still_honour_it():
    channel, bus = await started(relay_config(allowed_users=["U-owner"]))
    try:
        event = dm_event(user="U-someone-else", ts="3.0")
        # The router authorized this sender, so the lock does not apply to a relayed event...
        assert await channel.relay_event(envelope(event), argus("dm", trigger_user="U-someone-else")) is True
        inbound = await asyncio.wait_for(bus.get_inbound(), 2)
        assert inbound.user_id == "U-someone-else"
        # ...while the unrelayed handler still drops a non-allowed sender.
        assert await asyncio.to_thread(channel._handle_message_event, dict(event, ts="4.0")) is False
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_commands_count_only_in_the_dm_tier(public_tier_policy):
    channel, bus = await started()
    try:
        assert await channel.relay_event(envelope(dm_event(text="/help", ts="5.0")), argus("dm")) is True
        dm = await asyncio.wait_for(bus.get_inbound(), 2)
        event = {"type": "message", "channel": "C1", "user": "U-staff", "text": "/memory", "ts": "6.0", "thread_ts": "1.0"}
        assert await channel.relay_event(envelope(event), argus("public", trigger_user="U-staff")) is True
        public = await asyncio.wait_for(bus.get_inbound(), 2)
    finally:
        await channel.stop()
    assert dm.msg_type == InboundMessageType.COMMAND
    # In a channel thread a command would act on the owner's state; it stays chat text.
    assert public.msg_type == InboundMessageType.CHAT
    assert public.text == "/memory"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event", "block"),
    [
        (dm_event(), argus(tier="channel")),  # unknown tier fails closed
        (dm_event(), {"trigger_user": "U-owner"}),  # no tier
        (dm_event(), argus(action="escalate")),  # unknown action
        (dm_event(bot_id="B2"), argus()),  # bot traffic never triggers
        (dm_event(type="reaction_added"), argus()),
        (dm_event(text=""), argus()),
    ],
)
async def test_relay_event_refusals_publish_nothing(event, block):
    channel, bus = await started()
    try:
        assert await channel.relay_event(envelope(event), block) is False
        await asyncio.sleep(0)
        assert bus._inbound_queue.empty()
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_relay_stop_is_not_applied_and_says_why(caplog):
    channel, bus = await started()
    caplog.set_level(logging.INFO, logger="app.channels.slack")
    try:
        stop = envelope({**dm_event(), "type": "message", "ts": "1.0", "text": ""})
        assert await channel.relay_event(stop, argus(action="stop")) is False
        assert bus._inbound_queue.empty()
    finally:
        await channel.stop()
    assert "cannot cancel a channel run" in caplog.text


@pytest.mark.asyncio
async def test_relay_event_is_refused_outside_relay_mode():
    channel = SlackChannel(bus=MessageBus(), config={})
    channel._running = True
    assert await channel.relay_event(envelope(dm_event()), argus()) is False
    stopped = SlackChannel(bus=MessageBus(), config=relay_config())
    assert await stopped.relay_event(envelope(dm_event()), argus()) is False


@pytest.mark.asyncio
async def test_relayed_files_are_not_downloaded_with_the_router_key():
    channel, _ = await started()
    try:
        msg = InboundMessage(channel_name="slack", chat_id="D1", user_id="U-owner", text="see attached", files=[{"filename": "plan.pdf", "url_private_download": "https://files.slack.com/x"}])
        with patch.object(channel, "_download_private_file", AsyncMock()) as download:
            out = await channel.receive_file(msg, "thread-1")
        download.assert_not_called()
    finally:
        await channel.stop()
    assert out.files == []
    assert "plan.pdf (files are not relayed)" in out.text


# -- service: owner_email and credentials -------------------------------------------


def test_relay_slack_honours_owner_email_without_allowed_users():
    config = {
        "slack": {"transport": "relay", "owner_email": "Owner@Example.com"},
        "discord": {"transport": "relay", "owner_email": "owner@example.com"},  # relay means nothing here: still needs the lock
        "telegram": {"owner_email": "owner@example.com", "allowed_users": [42]},
    }
    assert channel_owner_emails_from_config(config) == {"slack": "owner@example.com", "telegram": "owner@example.com"}
    assert channel_owner_emails_from_config({"slack": {"owner_email": "owner@example.com"}}) == {}


def test_relay_credentials_are_the_router_url_and_key():
    assert _channel_has_credentials("slack", {"transport": "relay", "relay_url": RELAY_URL, "relay_token": RELAY_TOKEN})
    assert not _channel_has_credentials("slack", {"transport": "relay", "bot_token": "xoxb", "app_token": "xapp"})
    assert _channel_has_credentials("slack", {"bot_token": "xoxb", "app_token": "xapp"})
    assert not _channel_has_credentials("slack", {"relay_url": RELAY_URL, "relay_token": RELAY_TOKEN})


# -- POST /api/channels/slack/relay -------------------------------------------------

TOKEN_HEADER = internal_auth.INTERNAL_AUTH_HEADER_NAME
TEST_TOKEN = "test-internal-token"


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(router)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture()
def pinned_token():
    with patch.object(internal_auth, "_INTERNAL_AUTH_TOKEN", TEST_TOKEN):
        yield


def make_service(*, relay=True, running=True, accepted=True, channel=True):
    slack = MagicMock()
    slack.is_running = running
    slack.uses_relay_transport = relay
    slack.relay_event = AsyncMock(return_value=accepted)
    service = MagicMock()
    service.get_channel.return_value = slack if channel else None
    return service, slack


def post(client, *, token=TEST_TOKEN, body=None):
    headers = {TOKEN_HEADER: token} if token else {}
    payload = body if body is not None else {"envelope": envelope(dm_event()), "argus": argus()}
    return client.post("/api/channels/slack/relay", json=payload, headers=headers)


@pytest.mark.usefixtures("pinned_token")
class TestRelayRoute:
    @pytest.mark.parametrize("token", [None, "not-the-token"])
    def test_missing_or_invalid_internal_token_is_403(self, client, token):
        service, slack = make_service()
        with patch("app.channels.service.get_channel_service", return_value=service):
            resp = post(client, token=token)
        assert resp.status_code == 403
        slack.relay_event.assert_not_called()

    def test_valid_token_hands_the_event_to_the_channel(self, client):
        service, slack = make_service()
        body = {"envelope": envelope(dm_event()), "argus": argus()}
        with patch("app.channels.service.get_channel_service", return_value=service):
            resp = post(client, body=body)
        assert resp.status_code == 200
        assert resp.json() == {"accepted": True}
        service.get_channel.assert_called_once_with("slack")
        slack.relay_event.assert_awaited_once_with(body["envelope"], body["argus"])

    def test_not_accepted_is_reported(self, client):
        service, _ = make_service(accepted=False)
        with patch("app.channels.service.get_channel_service", return_value=service):
            resp = post(client)
        assert resp.status_code == 200
        assert resp.json() == {"accepted": False}

    @pytest.mark.parametrize("state", [{"relay": False}, {"running": False}, {"channel": False}])
    def test_404_unless_slack_runs_on_the_relay_transport(self, client, state):
        service, slack = make_service(**state)
        with patch("app.channels.service.get_channel_service", return_value=service):
            resp = post(client)
        assert resp.status_code == 404
        slack.relay_event.assert_not_called()

    def test_service_down_is_503(self, client):
        with patch("app.channels.service.get_channel_service", return_value=None):
            resp = post(client)
        assert resp.status_code == 503

    def test_body_needs_envelope_and_argus(self, client):
        service, slack = make_service()
        with patch("app.channels.service.get_channel_service", return_value=service):
            resp = post(client, body={"envelope": envelope(dm_event())})
        assert resp.status_code == 422
        slack.relay_event.assert_not_called()


@pytest.mark.asyncio
async def test_route_end_to_end_into_a_relay_channel(pinned_token):
    """The route feeds a real relay-mode SlackChannel, not only a double."""
    channel, bus = await started()
    service = MagicMock()
    service.get_channel.return_value = channel
    app = FastAPI()
    app.include_router(router)
    import httpx

    try:
        with patch("app.channels.service.get_channel_service", return_value=service):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as http:
                resp = await http.post("/api/channels/slack/relay", json={"envelope": envelope(dm_event()), "argus": argus()}, headers={TOKEN_HEADER: TEST_TOKEN})
        assert resp.status_code == 200 and resp.json() == {"accepted": True}
        inbound = await asyncio.wait_for(bus.get_inbound(), 2)
        assert inbound.agent_name == "atlas" and inbound.metadata["tier"] == "dm"
    finally:
        await channel.stop()

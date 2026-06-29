"""Tests for [argus patch #14] POST /api/channels/{name}/notify.

The endpoint injects a synthetic InboundMessage onto the channel bus so
scheduled jobs (morning briefing) ride the same pipeline as a real user
message. The auth boundary is the internal service token — never the SSO
session — because the caller chooses chat_id/user_id.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.gateway.internal_auth as internal_auth
from app.channels.message_bus import InboundMessage
from app.gateway.routers.channels import router

TOKEN_HEADER = internal_auth.INTERNAL_AUTH_HEADER_NAME
TEST_TOKEN = "test-internal-token"


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(router)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(autouse=True)
def _pin_internal_token():
    with patch.object(internal_auth, "_INTERNAL_AUTH_TOKEN", TEST_TOKEN):
        yield


def _make_service(*, channel_running: bool = True) -> MagicMock:
    service = MagicMock()
    service.get_channel.return_value = MagicMock() if channel_running else None
    service.bus.publish_inbound = AsyncMock()
    return service


def _post(client, *, token: str | None = TEST_TOKEN, **overrides):
    body = {"chat_id": "8726302666", "text": "Morning briefing"}
    body.update(overrides)
    headers = {TOKEN_HEADER: token} if token else {}
    return client.post("/api/channels/telegram/notify", json=body, headers=headers)


class TestNotifyAuth:
    def test_missing_token_is_403(self, client):
        service = _make_service()
        with patch("app.channels.service.get_channel_service", return_value=service):
            resp = _post(client, token=None)
        assert resp.status_code == 403
        service.bus.publish_inbound.assert_not_called()

    def test_wrong_token_is_403(self, client):
        service = _make_service()
        with patch("app.channels.service.get_channel_service", return_value=service):
            resp = _post(client, token="not-the-token")
        assert resp.status_code == 403
        service.bus.publish_inbound.assert_not_called()


class TestNotifyDispatch:
    def test_publishes_inbound_message(self, client):
        service = _make_service()
        with patch("app.channels.service.get_channel_service", return_value=service):
            resp = _post(client)
        assert resp.status_code == 200
        assert resp.json() == {"accepted": True, "channel": "telegram", "chat_id": "8726302666"}

        service.bus.publish_inbound.assert_awaited_once()
        msg = service.bus.publish_inbound.await_args.args[0]
        assert isinstance(msg, InboundMessage)
        assert msg.channel_name == "telegram"
        assert msg.chat_id == "8726302666"
        # user_id defaults to chat_id; topic_id defaults to None so private
        # chats reuse their persistent thread (store key "channel:chat_id").
        assert msg.user_id == "8726302666"
        assert msg.topic_id is None

    def test_explicit_user_and_topic_flow_through(self, client):
        service = _make_service()
        with patch("app.channels.service.get_channel_service", return_value=service):
            resp = _post(client, user_id="42", topic_id="msg-7")
        assert resp.status_code == 200
        msg = service.bus.publish_inbound.await_args.args[0]
        assert msg.user_id == "42"
        assert msg.topic_id == "msg-7"

    def test_text_is_stripped(self, client):
        service = _make_service()
        with patch("app.channels.service.get_channel_service", return_value=service):
            resp = _post(client, text="  briefing  ")
        assert resp.status_code == 200
        msg = service.bus.publish_inbound.await_args.args[0]
        assert msg.text == "briefing"


class TestNotifyValidation:
    def test_blank_text_is_422(self, client):
        service = _make_service()
        with patch("app.channels.service.get_channel_service", return_value=service):
            resp = _post(client, text="   ")
        assert resp.status_code == 422
        service.bus.publish_inbound.assert_not_called()

    def test_service_down_is_503(self, client):
        with patch("app.channels.service.get_channel_service", return_value=None):
            resp = _post(client)
        assert resp.status_code == 503

    def test_channel_not_running_is_404(self, client):
        service = _make_service(channel_running=False)
        with patch("app.channels.service.get_channel_service", return_value=service):
            resp = _post(client)
        assert resp.status_code == 404
        service.bus.publish_inbound.assert_not_called()

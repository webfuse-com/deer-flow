"""Tests for [argus patch #30] POST /api/playbooks/{schedule_id}/fire.

A reconciled Chronos channel_notify job fires here. The endpoint sources the
assembled prompt + the citizen's chat itself and publishes a synthetic
InboundMessage carrying the per-job agent (§3a) and memory policy (§4b). The
auth boundary is the internal service token (the same global token Chronos
holds) — never the SSO session — because the caller chooses the delivery target.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.gateway.internal_auth as internal_auth
import app.gateway.routers.playbooks as playbooks
from app.channels.message_bus import InboundMessage
from app.gateway.routers.playbooks import router

TOKEN_HEADER = internal_auth.INTERNAL_AUTH_HEADER_NAME
TEST_TOKEN = "test-internal-token"
SCHEDULE_ID = "morning-briefing"


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


@pytest.fixture()
def playbook_dir(tmp_path: Path):
    """Point the router at a tmp playbook dir holding one prompt."""
    (tmp_path / f"{SCHEDULE_ID}.md").write_text("Good morning. Today is {{TODAY}}.", encoding="utf-8")
    with patch.object(playbooks, "PLAYBOOK_DIR", tmp_path):
        yield tmp_path


def _make_service(*, channel_running: bool = True, chats: list[dict] | None = None) -> MagicMock:
    service = MagicMock()
    service.get_channel.return_value = MagicMock() if channel_running else None
    if chats is None:
        chats = [{"channel_name": "telegram", "chat_id": "8726302666", "user_id": "8726302666"}]
    service.store.list_entries.return_value = chats
    service.bus.publish_inbound = AsyncMock()
    return service


def _post(client, *, schedule_id: str = SCHEDULE_ID, token: str | None = TEST_TOKEN, body: dict | None = None):
    headers = {TOKEN_HEADER: token} if token else {}
    return client.post(f"/api/playbooks/{schedule_id}/fire", json=body or {}, headers=headers)


class TestFireAuth:
    def test_missing_token_is_403(self, client, playbook_dir):
        service = _make_service()
        with patch("app.channels.service.get_channel_service", return_value=service):
            resp = _post(client, token=None)
        assert resp.status_code == 403
        service.bus.publish_inbound.assert_not_called()

    def test_wrong_token_is_403(self, client, playbook_dir):
        service = _make_service()
        with patch("app.channels.service.get_channel_service", return_value=service):
            resp = _post(client, token="not-the-token")
        assert resp.status_code == 403
        service.bus.publish_inbound.assert_not_called()


class TestFireDispatch:
    def test_publishes_with_defaults(self, client, playbook_dir):
        """No body knobs → agent=atlas, memory=read-only, unattended, topic_id=None."""
        service = _make_service()
        with patch("app.channels.service.get_channel_service", return_value=service):
            resp = _post(client)
        assert resp.status_code == 200
        data = resp.json()
        assert data["accepted"] is True
        assert data["schedule_id"] == SCHEDULE_ID
        assert data["agent"] == "atlas"
        assert data["memory"] == "read-only"
        assert data["chats"] == 1

        service.bus.publish_inbound.assert_awaited_once()
        msg = service.bus.publish_inbound.await_args.args[0]
        assert isinstance(msg, InboundMessage)
        assert msg.channel_name == "telegram"
        assert msg.chat_id == "8726302666"
        assert msg.agent_name == "atlas"
        assert msg.unattended is True
        assert msg.memory_mode == "read-only"
        assert msg.topic_id is None
        assert msg.metadata["schedule_id"] == SCHEDULE_ID

    def test_date_placeholder_expanded(self, client, playbook_dir):
        from datetime import date

        service = _make_service()
        with patch("app.channels.service.get_channel_service", return_value=service):
            resp = _post(client)
        assert resp.status_code == 200
        msg = service.bus.publish_inbound.await_args.args[0]
        assert "{{TODAY}}" not in msg.text
        assert date.today().isoformat() in msg.text

    def test_per_job_agent_and_memory_flow_through(self, client, playbook_dir):
        service = _make_service()
        with patch("app.channels.service.get_channel_service", return_value=service):
            resp = _post(client, body={"agent": "pythia-internal", "memory": "read-write", "topic_id": "sched:morning-briefing"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent"] == "pythia-internal"
        assert data["memory"] == "read-write"
        msg = service.bus.publish_inbound.await_args.args[0]
        assert msg.agent_name == "pythia-internal"
        assert msg.memory_mode == "read-write"
        assert msg.topic_id == "sched:morning-briefing"

    def test_allowed_tools_flow_through(self, client, playbook_dir):
        """Per-run tool whitelist from the schedule frontmatter is forwarded on
        the InboundMessage so the tool policy filter can merge it with skills."""
        service = _make_service()
        with patch("app.channels.service.get_channel_service", return_value=service):
            resp = _post(client, body={"allowed_tools": ["calendar_list_events", "pythia_query"]})
        assert resp.status_code == 200
        msg = service.bus.publish_inbound.await_args.args[0]
        assert msg.allowed_tools == ["calendar_list_events", "pythia_query"]

    def test_allowed_tools_none_by_default(self, client, playbook_dir):
        """No allowed_tools in the request → msg.allowed_tools is None (legacy
        behavior: skill-based tool policy only)."""
        service = _make_service()
        with patch("app.channels.service.get_channel_service", return_value=service):
            resp = _post(client)
        assert resp.status_code == 200
        msg = service.bus.publish_inbound.await_args.args[0]
        assert msg.allowed_tools is None

    def test_fires_for_every_mapped_chat(self, client, playbook_dir):
        chats = [
            {"channel_name": "telegram", "chat_id": "111", "user_id": "111"},
            {"channel_name": "telegram", "chat_id": "222", "user_id": "u2"},
        ]
        service = _make_service(chats=chats)
        with patch("app.channels.service.get_channel_service", return_value=service):
            resp = _post(client)
        assert resp.status_code == 200
        assert resp.json()["chats"] == 2
        assert service.bus.publish_inbound.await_count == 2


class TestFireValidation:
    def test_unknown_schedule_is_404(self, client, playbook_dir):
        service = _make_service()
        with patch("app.channels.service.get_channel_service", return_value=service):
            resp = _post(client, schedule_id="does-not-exist")
        assert resp.status_code == 404
        service.bus.publish_inbound.assert_not_called()

    def test_invalid_memory_mode_is_422(self, client, playbook_dir):
        service = _make_service()
        with patch("app.channels.service.get_channel_service", return_value=service):
            resp = _post(client, body={"memory": "sometimes"})
        assert resp.status_code == 422
        service.bus.publish_inbound.assert_not_called()

    def test_body_schedule_id_mismatch_is_400(self, client, playbook_dir):
        service = _make_service()
        with patch("app.channels.service.get_channel_service", return_value=service):
            resp = _post(client, body={"schedule_id": "other"})
        assert resp.status_code == 400
        service.bus.publish_inbound.assert_not_called()

    def test_service_down_is_503(self, client, playbook_dir):
        with patch("app.channels.service.get_channel_service", return_value=None):
            resp = _post(client)
        assert resp.status_code == 503

    def test_channel_not_running_is_404(self, client, playbook_dir):
        service = _make_service(channel_running=False)
        with patch("app.channels.service.get_channel_service", return_value=service):
            resp = _post(client)
        assert resp.status_code == 404
        service.bus.publish_inbound.assert_not_called()

    def test_no_mapped_chat_is_409(self, client, playbook_dir):
        service = _make_service(chats=[])
        with patch("app.channels.service.get_channel_service", return_value=service):
            resp = _post(client)
        assert resp.status_code == 409
        service.bus.publish_inbound.assert_not_called()

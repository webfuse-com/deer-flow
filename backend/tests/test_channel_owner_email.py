"""[argus patch #113] channels.<name>.owner_email: a locked channel runs as its owner."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.channels.manager import ChannelManager
from app.channels.message_bus import InboundMessage, InboundMessageType, MessageBus
from app.channels.service import channel_owner_emails_from_config
from app.channels.store import JsonChannelStore


def test_owner_email_needs_an_allowed_users_lock():
    config = {
        "telegram": {"owner_email": "Owner@Example.com ", "allowed_users": [42]},
        "slack": {"owner_email": "owner@example.com"},  # no lock: ignored
        "feishu": {"owner_email": "$ATLAS_STACK_OWNER_EMAIL", "allowed_users": [1]},  # unexpanded
        "binding_store": "json",
    }
    assert channel_owner_emails_from_config(config) == {"telegram": "owner@example.com"}


class Repo:
    def __init__(self, users):
        self.users = users
        self.calls = 0

    async def get_user_by_email(self, email):
        self.calls += 1
        uid = self.users.get(email)
        return SimpleNamespace(id=uid) if uid else None


def manager(tmp_path: Path, monkeypatch, users):
    repo = Repo(users)
    monkeypatch.setattr("app.gateway.deps.get_user_repository", lambda: repo)
    monkeypatch.setattr("app.channels.manager._auth_disabled_owner_user_id", lambda: None)
    m = ChannelManager(
        bus=MessageBus(),
        store=JsonChannelStore(path=tmp_path / "store.json"),
        coalesce_window=0,
        channel_owner_emails={"telegram": "owner@example.com"},
    )
    return m, repo


@pytest.mark.asyncio
async def test_the_owner_is_resolved_once_and_an_unknown_email_is_retried_later(tmp_path, monkeypatch):
    m, repo = manager(tmp_path, monkeypatch, {"owner@example.com": "web-user-1"})
    assert await m._configured_owner_user_id("telegram") == "web-user-1"
    assert await m._configured_owner_user_id("telegram") == "web-user-1"
    assert repo.calls == 1
    assert await m._configured_owner_user_id("slack") is None

    m2, repo2 = manager(tmp_path, monkeypatch, {})
    clock = [1000.0]
    monkeypatch.setattr("app.channels.manager.time.monotonic", lambda: clock[0])
    assert await m2._configured_owner_user_id("telegram") is None
    assert await m2._configured_owner_user_id("telegram") is None and repo2.calls == 1
    repo2.users["owner@example.com"] = "web-user-2"  # the owner signs in to the web UI
    clock[0] += m2._OWNER_RETRY_SECONDS + 1
    assert await m2._configured_owner_user_id("telegram") == "web-user-2"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [InboundMessageType.CHAT, InboundMessageType.COMMAND])
async def test_every_message_on_the_channel_runs_as_the_owner(tmp_path, monkeypatch, kind):
    m, _ = manager(tmp_path, monkeypatch, {"owner@example.com": "web-user-1"})
    seen = []

    async def record(msg, **_kw):
        seen.append(msg.owner_user_id)

    monkeypatch.setattr(m, "_handle_chat", record)
    monkeypatch.setattr(m, "_handle_command", record)
    await m._handle_message(InboundMessage(channel_name="telegram", chat_id="42", user_id="42", text="hi", msg_type=kind))
    # a connection-bound owner still wins
    await m._handle_message(InboundMessage(channel_name="telegram", chat_id="42", user_id="42", text="hi", msg_type=kind, owner_user_id="bound-owner"))
    # another channel without an owner stays unowned
    await m._handle_message(InboundMessage(channel_name="slack", chat_id="C1", user_id="U1", text="hi", msg_type=kind))
    assert seen == ["web-user-1", "bound-owner", None]

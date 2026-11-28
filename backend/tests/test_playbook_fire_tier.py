"""[argus patch #179] A playbook fire may name the ``event`` run tier.

Chronos wakes a person's Atlas when a company-bus event arrives for one of
their subscriptions. The event's text is another citizen's app's data, so the
run gets a ceiling the caller cannot widen: the image-held tier policy (#174)
under a tier of its own, ``event``. Before this patch the fire request had no
``tier`` field: pydantic dropped the key and the run was the owner's, with
every tool, whatever Chronos sent.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.gateway.internal_auth as internal_auth
import app.gateway.routers.playbooks as playbooks
from app.channels.message_bus import InboundMessage, MessageBus
from app.gateway.routers.playbooks import router
from deerflow.agents import tier_policy

TOKEN = "test-internal-token"
EVENT_AGENT = "atlas-event"
TOPIC = "bus:hubspot-deals:deal-won"
POLICY = {
    "version": 1,
    "tiers": {
        "event": {
            "agent": EVENT_AGENT,
            "memory_mode": "off",
            "subagents": False,
            "skills": [],
            "pythia_ring_max": "internal",
            "tools": ["pythia_query", "atlas_action_stage"],
            "mcp_servers": {"pythia": "http://argus-kb-api:8000/pythia/mcp"},
            "disable_clarification": True,
        }
    },
}
# What a citizen's own channel session asks for; the policy must win over it.
CITIZEN_SESSION = {"telegram": {"context": {"memory_mode": "read-write", "subagent_enabled": True, "user_email": "owner@example.com"}}}


@pytest.fixture()
def policy_path(tmp_path, monkeypatch):
    path = tmp_path / "argus-tier-policy.json"
    monkeypatch.setattr(tier_policy, "TIER_POLICY_PATH", path)
    tier_policy.reset_tier_policy_cache()
    yield path
    tier_policy.reset_tier_policy_cache()


@pytest.fixture()
def agents(monkeypatch):
    present = {EVENT_AGENT, "atlas"}
    monkeypatch.setattr(tier_policy, "tier_agent_present", lambda name, *, user_id=None: name in present)
    return present


@pytest.fixture()
def event_policy(policy_path, agents):
    policy_path.write_text(json.dumps(POLICY))
    tier_policy.reset_tier_policy_cache()
    return policy_path


@pytest.fixture()
def service():
    svc = MagicMock()
    svc.get_channel.return_value = SimpleNamespace(uses_relay_transport=False)
    svc.store.list_entries = AsyncMock(return_value=[{"channel_name": "telegram", "chat_id": "8726302666", "user_id": "8726302666"}])
    svc.bus.publish_inbound = AsyncMock()
    return svc


@pytest.fixture()
def client(service):
    app = FastAPI()
    app.include_router(router)
    with (
        patch.object(internal_auth, "_INTERNAL_AUTH_TOKEN", TOKEN),
        patch("app.channels.service.get_channel_service", return_value=service),
        TestClient(app, raise_server_exceptions=False) as c,
    ):
        yield c


def fire(client, **body):
    body = {"prompt_text": 'A deal was won: {"deal": 41}', **body}
    return client.post("/api/playbooks/hook-deal-won/fire", json=body, headers={internal_auth.INTERNAL_AUTH_HEADER_NAME: TOKEN})


def published(service) -> InboundMessage:
    service.bus.publish_inbound.assert_awaited_once()
    return service.bus.publish_inbound.await_args.args[0]


# -- the fire route ---------------------------------------------------------------


def test_an_event_fire_runs_what_the_policy_says_and_the_answer_names_it(client, service, event_policy):
    resp = fire(client, tier="event", topic_id=TOPIC, agent="atlas", memory="read-write", allowed_tools=["gmail_send"])
    assert resp.status_code == 200
    data = resp.json()
    # The caller reads back that the gateway understood the tier, and what runs.
    assert data["tier"] == "event" and data["agent"] == EVENT_AGENT and data["memory"] == "off"
    msg = published(service)
    assert msg.metadata["argus_tier"] == "event" and msg.metadata["source"] == "playbook"
    assert msg.agent_name == EVENT_AGENT and msg.memory_mode == "off" and msg.topic_id == TOPIC
    # The route loaded the policy off the loop, so the manager's cache read finds it.
    assert tier_policy.cached_policy_for("event") is not None


def test_an_untiered_fire_is_as_before(client, service, event_policy):
    resp = fire(client, agent="atlas", memory="read-write")
    assert resp.status_code == 200 and resp.json()["tier"] is None
    assert resp.json()["agent"] == "atlas" and resp.json()["memory"] == "read-write"
    msg = published(service)
    assert "argus_tier" not in msg.metadata and msg.agent_name == "atlas" and msg.memory_mode == "read-write"


@pytest.mark.parametrize("tier", ["public", "private", "owner", "dm", "Event", ""])
def test_only_the_event_tier_may_be_asked_for(client, service, event_policy, tier):
    # A channel tier is bound by the Slack relay from where a message was written, never by a caller.
    assert fire(client, tier=tier, topic_id=TOPIC).status_code == 422
    service.bus.publish_inbound.assert_not_called()


def test_an_event_fire_needs_a_topic_of_its_own(client, service, event_policy):
    # Admission binds the tier to the thread: without a topic that is the person's one chat thread.
    resp = fire(client, tier="event")
    assert resp.status_code == 422 and "topic_id" in resp.json()["detail"]
    assert fire(client, tier="event", topic_id="  ").status_code == 422
    service.bus.publish_inbound.assert_not_called()


def test_an_event_fire_is_refused_in_an_existing_web_chat(client, service, event_policy):
    resp = fire(client, tier="event", thread_id="thread-mine", owner_email="owner@example.com")
    assert resp.status_code == 422 and "existing web chat" in resp.json()["detail"]
    service.bus.publish_inbound.assert_not_called()


def test_no_policy_for_the_tier_is_refused_with_the_reason(client, service, policy_path, agents):
    # No file at all.
    resp = fire(client, tier="event", topic_id=TOPIC)
    assert resp.status_code == 409 and "no valid policy" in resp.json()["detail"]
    # A file that covers other tiers only.
    policy_path.write_text(json.dumps({"version": 1, "tiers": {}}))
    tier_policy.reset_tier_policy_cache()
    assert fire(client, tier="event", topic_id=TOPIC).status_code == 409
    # An entry the loader refuses (no tools: the ceiling is required).
    broken = {"version": 1, "tiers": {"event": {k: v for k, v in POLICY["tiers"]["event"].items() if k != "tools"}}}
    policy_path.write_text(json.dumps(broken))
    tier_policy.reset_tier_policy_cache()
    assert fire(client, tier="event", topic_id=TOPIC).status_code == 409
    service.bus.publish_inbound.assert_not_called()


def test_a_policy_agent_that_is_not_on_the_stack_is_refused(client, service, event_policy, agents):
    agents.discard(EVENT_AGENT)
    resp = fire(client, tier="event", topic_id=TOPIC)
    assert resp.status_code == 409 and repr(EVENT_AGENT) in resp.json()["detail"]
    service.bus.publish_inbound.assert_not_called()


def test_a_slack_delivery_keeps_its_conversation_kind_beside_the_run_tier(client, service, event_policy):
    owner = SimpleNamespace(enabled=True, dm="D-owner", user_id="U-owner", channels=("slack",))
    slack = SimpleNamespace(uses_relay_transport=True, owner_conversation=AsyncMock(return_value=(owner, "")))
    service.get_channel.return_value = slack
    assert fire(client, tier="event", topic_id=TOPIC, channel="slack").status_code == 200
    msg = published(service)
    assert msg.metadata["tier"] == "dm", "where the answer is posted"
    assert msg.metadata["argus_tier"] == "event", "what the run may do"


def test_a_fresh_web_chat_carries_the_tier_in_its_run_context(event_policy):
    app = FastAPI()
    app.include_router(router)
    users = MagicMock()
    users.get_user_by_email = AsyncMock(return_value=SimpleNamespace(id="owner-1"))
    launch = AsyncMock(return_value={"run_id": "r1", "thread_id": "t"})
    with (
        patch.object(internal_auth, "_INTERNAL_AUTH_TOKEN", TOKEN),
        patch("app.gateway.deps.get_user_repository", return_value=users),
        patch("app.gateway.deps.get_thread_store", return_value=MagicMock()),
        patch("app.gateway.services.launch_scheduled_thread_run", launch),
        TestClient(app, raise_server_exceptions=False) as c,
    ):
        resp = fire(c, tier="event", fresh=True, owner_email="owner@example.com", memory="read-write", allowed_tools=["gmail_send"])
    assert resp.status_code == 200 and resp.json()["tier"] == "event" and resp.json()["channel"] == "web"
    kwargs = launch.await_args.kwargs
    # Admission (start_run) reads the tier from the context and applies the policy last.
    assert kwargs["context"]["argus_tier"] == "event" and kwargs["assistant_id"] == EVENT_AGENT
    assert kwargs["context"]["memory_mode"] == "off" and len(kwargs["thread_id"]) == 36


# -- what the tier does to the run ---------------------------------------------------


def test_the_channel_manager_binds_the_fired_tier_and_the_policy_wins(client, service, event_policy):
    from app.channels.manager import ChannelManager
    from app.channels.store import JsonChannelStore

    assert fire(client, tier="event", topic_id=TOPIC, memory="read-write", allowed_tools=["gmail_send"]).status_code == 200
    manager = ChannelManager(bus=MessageBus(), store=JsonChannelStore(path=Path(tempfile.mkdtemp()) / "store.json"), channel_sessions=CITIZEN_SESSION)
    _, run_config, run_context = manager._resolve_run_params(published(service), "thread-bus")
    for carrier in (run_context, run_config["configurable"]):
        assert carrier["agent_name"] == EVENT_AGENT and carrier["memory_mode"] == "off"
        assert carrier["subagent_enabled"] is False
        assert "allowed_tools" not in carrier and "user_email" not in carrier
    assert run_context["argus_tier"] == "event" and run_context["disable_clarification"] is True
    assert "argus_tier" not in run_config["configurable"], "runtime only, never checkpointed"


class _Threads:
    """A thread store as run admission uses it."""

    def __init__(self, metadata: dict | None = None):
        self.metadata = dict(metadata or {})

    async def get(self, thread_id, *, user_id=None):
        return {"thread_id": thread_id, "metadata": dict(self.metadata)}

    async def update_metadata(self, thread_id, metadata, *, touch=False, user_id=None):
        self.metadata.update(metadata)


@pytest.mark.asyncio
async def test_admission_binds_the_event_tier_to_its_topics_thread(event_policy):
    threads = _Threads()
    config = {"context": {"argus_tier": "event", "agent_name": "atlas", "memory_mode": "read-write"}, "configurable": {}}
    policy = await tier_policy.admit_run_tier(config, threads, "thread-bus", {})
    assert policy is not None and policy.tier == "event" and policy.thread_bound
    assert config["context"]["agent_name"] == EVENT_AGENT and config["context"]["memory_mode"] == "off"
    assert threads.metadata["argus_tier"] == "event"
    # A later run there that names no tier, or the owner tier, stays at the event ceiling:
    # which is why a fire without a topic of its own is refused.
    for later in ({}, {"argus_tier": "owner"}):
        again = {"context": dict(later), "configurable": {}}
        assert (await tier_policy.admit_run_tier(again, threads, "thread-bus", {})).tier == "event"
    # The header every MCP call of the run carries.
    assert tier_policy.header_tier_value("event") == "event"


def test_the_event_tier_among_the_others():
    assert "event" in tier_policy.POLICY_TIERS and tier_policy.EVENT_TIER == "event"
    assert tier_policy.is_restricted_tier("event")
    assert tier_policy.stricter_tier("owner", "event") == "event" and tier_policy.stricter_tier("dm", "event") == "event"
    # A channel's own thread keeps the channel's audience rule.
    assert tier_policy.stricter_tier("event", "private") == "private"
    assert tier_policy.stricter_tier("event", "public") == "public"
    # An unknown tier still wins over every known one, and is then refused.
    assert tier_policy.stricter_tier("public", "weird") == "weird" and tier_policy.stricter_tier("event", "weird") == "weird"
    assert playbooks._ARGUS_TIER_KEY == tier_policy.ARGUS_TIER_KEY


def test_the_event_policy_has_the_public_shape(event_policy):
    policy = tier_policy.policy_for("event")
    assert policy.agent == EVENT_AGENT and policy.tools == frozenset({"pythia_query", "atlas_action_stage"})
    assert policy.memory_mode == "off" and policy.subagents is False and not policy.reads_memory
    assert policy.taint is None, "no taint rules: a ceiling, as the public tier"

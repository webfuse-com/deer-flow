"""[argus patch #174, #175] Regression anchor: the run-tier paths never touch the policy file on the event loop.

``argus-tier-policy.json`` is checked (``stat``) and read on every run
admission, on every MCP call from a restricted run, by the capability probe and
when the relay admits a restricted tier. Each of those runs on the Gateway event
loop, so the file access has to happen in a worker thread. These tests drive the
real code with a real policy file under the strict Blockbuster gate; a lookup
that regresses onto the loop raises ``BlockingError`` from ``os.stat``. The
deployment server list and the agent lookup are replaced by functions that read
a real file too, so moving either back onto the loop trips the gate as well.

[argus patch #175] The owner tier adds the same paths (admission, MCP calls,
the relay's agent check, the probe) plus the taint guard and the flagged post,
which are pure and must stay so.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import ToolException
from langchain_mcp_adapters.interceptors import MCPToolCallRequest
from langgraph.store.memory import InMemoryStore

from app.channels.message_bus import InboundMessage, MessageBus, OutboundMessage
from app.channels.slack import SlackChannel
from deerflow.agents import tier_policy
from deerflow.config.extensions_config import ExtensionsConfig, McpServerConfig
from deerflow.mcp.run_tier import build_run_tier_interceptor
from deerflow.persistence.thread_meta.memory import MemoryThreadMetaStore
from deerflow.runtime.context_keys import CURRENT_RUN_PRE_EXISTING_MESSAGE_IDS_KEY

pytestmark = pytest.mark.asyncio

PYTHIA_URL = "http://argus-kb-api:8000/pythia/mcp"
POLICY = {
    "_comment": ["shipped by the overlay image"],
    "version": 1,
    "tiers": {
        "public": {
            "agent": "atlas-slack-public",
            "tools": ["pythia_query", "web_search"],
            "mcp_servers": {"pythia": PYTHIA_URL},
            "prompt": "You are {persona}.",
        },
        "owner": {
            "mcp_servers": {"pythia": PYTHIA_URL},
            "public_tools": ["pythia_query", "web_search"],
            "private_read_tools": ["calendar_read"],
            "self_write_tools": [],
            "write_tools": ["asana_create_task"],
            "egress_tools": ["web_search"],
        },
    },
}


@pytest.fixture()
def policy_file(tmp_path, monkeypatch):
    path = tmp_path / "argus-tier-policy.json"
    path.write_text(json.dumps(POLICY))
    monkeypatch.setattr(tier_policy, "TIER_POLICY_PATH", path)
    tier_policy.reset_tier_policy_cache()

    # Stand-ins that do real file IO, so running them on the loop trips the gate.
    servers = tmp_path / "servers.json"
    servers.write_text(json.dumps({"pythia": PYTHIA_URL}))
    agents = tmp_path / "agents.json"
    agents.write_text(json.dumps(["atlas-slack-public", "atlas"]))
    monkeypatch.setattr(tier_policy, "_global_mcp_server_urls", lambda: json.loads(servers.read_text()))
    monkeypatch.setattr(tier_policy, "tier_agent_present", lambda name, *, user_id=None: name in json.loads(agents.read_text()))
    yield path
    tier_policy.reset_tier_policy_cache()


async def test_admission_reads_the_policy_and_the_thread_tier_off_loop(policy_file) -> None:
    thread_store = MemoryThreadMetaStore(InMemoryStore())
    await thread_store.create("thread-pub", user_id="owner-1", metadata={"argus_tier": "public"})
    config = {"configurable": {"agent_name": "atlas"}, "context": {"agent_name": "atlas", "user_email": "owner@example.com"}}

    policy = await tier_policy.admit_run_tier(config, thread_store, "thread-pub", {})

    assert policy is not None and policy.tier == "public"
    assert config["context"]["agent_name"] == "atlas-slack-public"
    assert "user_email" not in config["context"]


async def test_admission_of_an_untiered_run_stays_off_loop(policy_file) -> None:
    thread_store = MemoryThreadMetaStore(InMemoryStore())
    await thread_store.create("thread-own", user_id="owner-1")
    config = {"configurable": {}, "context": {"agent_name": "atlas"}}
    assert await tier_policy.admit_run_tier(config, thread_store, "thread-own", {}) is None


def _request(server_name: str, tier: str | None):
    return MCPToolCallRequest(name="query", args={}, server_name=server_name, headers=None, runtime=SimpleNamespace(context={"argus_tier": tier} if tier else {}))


async def test_mcp_interceptor_checks_restricted_calls_off_loop(policy_file) -> None:
    # Discovery-time registration reads the presence an off-loop check recorded.
    await asyncio.to_thread(tier_policy.tier_policy_file_present)
    config = ExtensionsConfig(
        mcp_servers={
            "pythia": McpServerConfig(enabled=True, type="http", url=PYTHIA_URL),
            "gmail": McpServerConfig(enabled=True, type="http", url="http://argus-atlas-mcp:8000/x/gmail/mcp"),
        },
        skills={},
    )
    interceptor = build_run_tier_interceptor(config)
    seen = []

    async def transport(request):
        seen.append(dict(request.headers or {}))
        return "result"

    assert await interceptor(_request("pythia", "public"), transport) == "result"
    with pytest.raises(ToolException):
        await interceptor(_request("gmail", "public"), transport)
    # An owner call reads only the run context.
    assert await interceptor(_request("gmail", None), transport) == "result"
    assert [headers["X-Argus-Run-Tier"] for headers in seen] == ["public", "owner"]


async def test_capability_probe_reads_the_policy_off_loop(policy_file) -> None:
    channel = SlackChannel(bus=MessageBus(), config={"transport": "relay", "relay_url": "http://slack-router:8080/api/", "relay_token": "k", "tier_agents": {"dm": "atlas", "public": "atlas"}})

    capabilities = await channel.relay_capabilities()

    assert capabilities["tiers"] == ["dm", "public", "owner"]
    assert capabilities["policy"]["public_agent"] == "atlas-slack-public"
    assert capabilities["policy"]["agent_present"] is True


@pytest.fixture()
def manager():
    from app.channels.manager import ChannelManager
    from app.channels.store import JsonChannelStore

    return ChannelManager(bus=MessageBus(), store=JsonChannelStore(path=Path(tempfile.mkdtemp()) / "store.json"), channel_sessions={"slack": {"context": {"agent_name": "atlas", "user_email": "owner@example.com"}}})


async def test_manager_applies_the_relay_loaded_policy_without_file_access(policy_file, manager) -> None:
    # The relay loads the policy off the loop before a message reaches the manager.
    await tier_policy.aload_tier_policies()
    msg = InboundMessage(channel_name="slack", chat_id="C1", user_id="U1", text="hi", thread_ts="1.0", metadata={"argus_tier": "public", "persona": "Atlas for Nicholas"})

    _, _, run_context = manager._resolve_run_params(msg, "thread-1")

    assert run_context["agent_name"] == "atlas-slack-public"
    assert "user_email" not in run_context
    assert run_context["argus_tier_vars"] == {"persona": "Atlas for Nicholas"}


# -- [argus patch #175] the owner tier --------------------------------------------


async def test_owner_admission_stays_off_loop(policy_file) -> None:
    thread_store = MemoryThreadMetaStore(InMemoryStore())
    await thread_store.create("thread-c", user_id="owner-1")
    config = {"configurable": {}, "context": {"argus_tier": "owner", "agent_name": "atlas"}}
    policy = await tier_policy.admit_run_tier(config, thread_store, "thread-c", {})
    assert policy is not None and policy.tier == "owner"
    assert config["context"]["memory_mode"] == "read-only"


async def test_owner_mcp_calls_decide_off_loop(policy_file) -> None:
    await asyncio.to_thread(tier_policy.tier_policy_file_present)
    config = ExtensionsConfig(
        mcp_servers={
            "pythia": McpServerConfig(enabled=True, type="http", url=PYTHIA_URL),
            "gmail": McpServerConfig(enabled=True, type="http", url="http://argus-atlas-mcp:8000/x/gmail/mcp"),
        },
        skills={},
    )
    interceptor = build_run_tier_interceptor(config)
    seen = {}

    async def transport(request):
        seen[request.server_name] = dict(request.headers or {})
        return "result"

    for server in ("gmail", "pythia"):
        request = MCPToolCallRequest(name="q", args={}, server_name=server, headers={"X-Pythia-Caller-Token": "t"}, runtime=SimpleNamespace(context={"argus_tier": "owner"}))
        assert await interceptor(request, transport) == "result"
    assert seen["gmail"]["X-Pythia-Caller-Token"] == "t" and "X-Pythia-Caller-Token" not in seen["pythia"]


async def test_the_probe_checks_the_owner_tier_off_loop(policy_file) -> None:
    channel = SlackChannel(bus=MessageBus(), config={"transport": "relay", "relay_url": "http://slack-router:8080/api/", "relay_token": "k", "tier_agents": {"dm": "atlas"}, "session": {"context": {"agent_name": "atlas"}}})
    assert (await channel.relay_capabilities())["tiers"] == ["dm", "owner"]


async def test_the_taint_guard_and_the_flagged_post_do_no_io(policy_file) -> None:
    policy = await asyncio.to_thread(tier_policy.policy_for, "owner")
    guard = await asyncio.to_thread(lambda: tier_policy.guard_tier_middlewares([], [SimpleNamespace(name="asana_create_task"), SimpleNamespace(name="calendar_read")], policy)[0])
    messages = [HumanMessage(content="now", id="h1"), AIMessage(content="", id="a1", tool_calls=[{"name": "calendar_read", "args": {}, "id": "c1"}, {"name": "asana_create_task", "args": {}, "id": "c2"}])]
    runtime = SimpleNamespace(context={CURRENT_RUN_PRE_EXISTING_MESSAGE_IDS_KEY: frozenset({"h1"})})
    request = SimpleNamespace(tool_call={"name": "asana_create_task", "id": "c2", "args": {}}, state={"messages": messages}, runtime=runtime)

    async def handler(req):
        return ToolMessage(content="ok", tool_call_id="c2")

    refused = await guard.awrap_tool_call(request, handler)
    assert refused.content == tier_policy.TAINT_REFUSAL
    assert guard.after_model({"messages": messages}, runtime)["messages"][0].additional_kwargs[tier_policy.TURN_PRIVATE_KEY] == ["calendar"]

    class Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.calls = []

        def chat_postMessage(self, **kwargs):
            self.calls.append(kwargs)
            return {"ok": True, "ts": "1.1"}

        def reactions_add(self, **kwargs):
            return {"ok": True}

        def reactions_remove(self, **kwargs):
            return {"ok": True}

        def chat_delete(self, **kwargs):
            return {"ok": True}

    clients = []

    def factory(**kwargs):
        clients.append(Client(**kwargs))
        return clients[-1]

    channel = SlackChannel(bus=MessageBus(), config={"transport": "relay", "relay_url": "http://slack-router:8080/api/", "relay_token": "k", "web_client_factory": factory})
    channel._web_client = factory(token="k", base_url="http://slack-router:8080/api/")
    await channel.send(OutboundMessage(channel_name="slack", chat_id="C1", thread_id="t", text="Board at 9.", thread_ts="1.0", metadata={tier_policy.TURN_PRIVATE_KEY: ["calendar"]}))
    assert clients[-1].kwargs["headers"] == {"X-Argus-Turn-Private": "1", "X-Argus-Turn-Sources": "calendar"}

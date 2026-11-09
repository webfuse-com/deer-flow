"""[argus patch #175] The owner tier: the owner's own Atlas in a Slack channel, with taint gating.

The router sends tier ``owner`` only for a channel run it verified its owner
triggered. The run keeps the owner's agent and tools (minus subagents and
``update_agent``), reads memory without writing it, and caps Pythia at the
internal ring. Tools are classified by ``tiers.owner`` in the image's policy
file: PUBLIC, WRITE, or (by default) PRIVATE. The first PRIVATE read taints the
run: its WRITE and egress calls are refused, its AI messages are stamped, and
the Slack adapter sends its answer with ``X-Argus-Turn-Private`` so the router
drafts it to the owner.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.store.memory import InMemoryStore

import app.gateway.internal_auth as internal_auth
from app.channels.message_bus import InboundMessage, MessageBus, OutboundMessage, ResolvedAttachment
from app.channels.slack import SlackChannel
from app.channels.store import JsonChannelStore
from deerflow.agents import tier_policy
from deerflow.agents.memory.write_policy import memory_write_allowed
from deerflow.agents.middlewares.tier_tool_guard_middleware import TierToolGuardMiddleware
from deerflow.config.agents_config import AgentConfig
from deerflow.config.extensions_config import ExtensionsConfig, McpServerConfig
from deerflow.persistence.thread_meta.memory import MemoryThreadMetaStore
from deerflow.runtime.context_keys import CURRENT_RUN_PRE_EXISTING_MESSAGE_IDS_KEY
from deerflow.tools.mcp_metadata import tag_mcp_tool

PYTHIA_URL = "http://argus-kb-api:8000/pythia/mcp"
DB_URL = "http://argus-kb-api:8000/pythia/db/mcp"
GMAIL_URL = "http://argus-atlas-mcp:8000/atlas-nicholas/gmail/mcp"
CALENDAR_URL = "http://argus-atlas-mcp:8000/atlas-nicholas/calendar/mcp"
ASANA_URL = "http://argus-atlas-mcp:8000/atlas-nicholas/asana/mcp"
GLOBAL_MCP = {"pythia": PYTHIA_URL, "db": DB_URL, "gmail": GMAIL_URL, "calendar": CALENDAR_URL, "asana": ASANA_URL}

OWNER_POLICY = {
    "memory_mode": "read-only",
    "pythia_ring_max": "internal",
    "mcp_servers": {"pythia": PYTHIA_URL},
    "public_tools": ["pythia_query", "slack_channel_history", "slack_search", "web_search", "web_search_exa", "tool_search", "describe_skill", "ask_clarification"],
    "private_read_tools": ["calendar_list_events", "gmail_search"],
    # A subset of write_tools by design: a write before taint, still allowed after it.
    "self_write_tools": ["gmail_create_draft", "drive_create_doc"],
    "write_tools": ["asana_create_task", "asana_create_project", "github_create_issue", "github_add_comment", "gmail_create_draft", "drive_create_doc", "drive_update_doc"],
    "egress_tools": ["web_search", "web_search_exa", "image_search", "web_fetch"],
}
POLICY = {
    "_comment": ["shipped by the acropolis overlay image"],
    "version": 1,
    "tier_only_tools": ["slack_channel_history", "slack_search"],
    "tiers": {
        "public": {"agent": "atlas-slack-public", "tools": ["pythia_query", "web_search"], "mcp_servers": {"pythia": PYTHIA_URL}},
        "owner": OWNER_POLICY,
    },
}


def _write_policy(path: Path, policy: object) -> None:
    path.write_text(policy if isinstance(policy, str) else json.dumps(policy))
    tier_policy.reset_tier_policy_cache()


@pytest.fixture()
def policy_path(tmp_path, monkeypatch):
    path = tmp_path / "argus-tier-policy.json"
    monkeypatch.setattr(tier_policy, "TIER_POLICY_PATH", path)
    monkeypatch.setattr(tier_policy, "_global_mcp_server_urls", lambda: dict(GLOBAL_MCP))
    tier_policy.reset_tier_policy_cache()
    yield path
    tier_policy.reset_tier_policy_cache()


@pytest.fixture()
def agents_present(monkeypatch):
    present = {"atlas", "atlas-slack-public", "atlas-channel"}
    monkeypatch.setattr(tier_policy, "tier_agent_present", lambda name, *, user_id=None: name in present)
    return present


@pytest.fixture()
def owner_policy(policy_path, agents_present):
    _write_policy(policy_path, POLICY)
    return tier_policy.policy_for("owner")


def _mcp(name: str, server: str):
    @tool(name)
    def remote(query: str = "") -> str:
        """An MCP tool."""
        return query

    return tag_mcp_tool(remote, server_name=server, transport="http")


class NamedTool:
    def __init__(self, name: str):
        self.name = name


# -- the policy -----------------------------------------------------------------


def test_owner_tier_parses_with_defaults(owner_policy):
    assert owner_policy.tier == "owner"
    assert owner_policy.agent is None and owner_policy.tools is None and owner_policy.skills is None
    assert not owner_policy.has_ceiling and not owner_policy.thread_bound
    assert owner_policy.memory_mode == "read-only" and owner_policy.reads_memory
    assert owner_policy.pythia_ring_max == "internal"
    assert owner_policy.taint.write_tools >= {"asana_create_task", "gmail_create_draft"}
    assert owner_policy.taint.private_read_tools == {"calendar_list_events", "gmail_search"}
    assert owner_policy.taint.self_write_tools == {"gmail_create_draft", "drive_create_doc"}
    assert "user_email" in owner_policy.strip_context
    assert tier_policy.is_restricted_tier("owner")


@pytest.mark.parametrize(
    "change",
    [
        {"public_tools": None},
        {"private_read_tools": None},
        {"self_write_tools": None},
        {"write_tools": None},
        {"egress_tools": None},
        {"self_write_tools": "gmail_create_draft"},
        {"subagents": True},
        {"memory_mode": "read-write"},
        {"pythia_ring_max": "everything"},
        {"public_tools": "web_search"},
    ],
)
def test_a_bad_owner_entry_refuses_the_owner_tier_only(policy_path, change):
    policy = copy.deepcopy(POLICY)
    for key, value in change.items():
        if value is None:
            del policy["tiers"]["owner"][key]
        else:
            policy["tiers"]["owner"][key] = value
    _write_policy(policy_path, policy)
    assert tier_policy.policy_for("owner") is None
    assert tier_policy.policy_for("public") is not None


def test_owner_keys_for_a_ceiling_are_ignored(policy_path, caplog):
    policy = copy.deepcopy(POLICY)
    policy["tiers"]["owner"].update({"agent": "atlas-slack-public", "tools": ["web_search"], "_note": "doc"})
    _write_policy(policy_path, policy)
    owner = tier_policy.policy_for("owner")
    assert owner.agent is None and owner.tools is None
    assert "agent, tools" in caplog.text


def test_memory_read_only_can_be_switched_off(policy_path):
    policy = copy.deepcopy(POLICY)
    policy["tiers"]["owner"]["memory_mode"] = "off"
    _write_policy(policy_path, policy)
    assert not tier_policy.policy_for("owner").reads_memory


def test_the_owner_tier_ranks_below_public():
    assert tier_policy.stricter_tier("owner", "public") == "public"
    assert tier_policy.stricter_tier("dm", "owner") == "owner"
    assert tier_policy.stricter_tier(None, "owner") == "owner"


# -- classification: default deny ------------------------------------------------


def test_classification(owner_policy):
    classify = tier_policy.classify_tool
    # WRITE by name, for both write lists (writes never taint).
    assert classify(_mcp("asana_create_task", "asana"), owner_policy) == tier_policy.ToolClass("write", "asana")
    assert classify(_mcp("gmail_create_draft", "gmail"), owner_policy) == tier_policy.ToolClass("write", "gmail")
    # PUBLIC: listed in-process tools (infra tools included) and a listed MCP tool from an allowlisted URL.
    for name in ("web_search", "tool_search", "describe_skill", "ask_clarification"):
        assert classify(NamedTool(name), owner_policy).kind == "public"
    assert classify(_mcp("pythia_query", "pythia"), owner_policy).kind == "public"
    # PRIVATE by default: private reads, an unknown tool, any other MCP tool, a lookalike, an alias.
    assert classify(_mcp("calendar_list_events", "calendar"), owner_policy) == tier_policy.ToolClass("private", "calendar")
    assert classify(NamedTool("bash"), owner_policy) == tier_policy.ToolClass("private", "bash")
    assert classify(NamedTool("brand_new_tool"), owner_policy) == tier_policy.ToolClass("private", "brand")
    for name in ("present_files", "view_image"):
        assert classify(NamedTool(name), owner_policy).kind == "private"
    assert classify(_mcp("gmail_send_message", "gmail"), owner_policy) == tier_policy.ToolClass("private", "gmail")
    assert classify(NamedTool("pythia_query"), owner_policy).kind == "private"  # in-process lookalike
    assert classify(_mcp("web_search", "personal-search"), owner_policy).kind == "private"  # MCP, not from a listed URL


def test_the_after_taint_allowlist(owner_policy):
    taint = tier_policy.guard_tier_middlewares([], [NamedTool("web_search"), NamedTool("tool_search"), _mcp("pythia_query", "pythia")], owner_policy)[0].taint
    permitted = taint.permitted_after_taint
    # PUBLIC minus egress, private reads, self-writes.
    for name in ("pythia_query", "tool_search", "calendar_list_events", "gmail_search", "gmail_create_draft", "drive_create_doc"):
        assert permitted(name), name
    # Egress even when PUBLIC, writes, and everything unclassified.
    for name in ("web_search", "web_search_exa", "web_fetch", "asana_create_task", "drive_update_doc", "bash", "browser_navigate", "gmail_send_message", "present_files", "never_heard_of_it", None):
        assert not permitted(name), name


def test_self_writes_stay_allowed_though_they_are_writes_and_egress_beats_everything(policy_path, caplog):
    policy = copy.deepcopy(POLICY)
    owner = policy["tiers"]["owner"]
    owner["write_tools"] = ["github_create_issue", "gmail_create_draft"]
    owner["self_write_tools"] = ["gmail_create_draft", "web_fetch", "slack_post_draft"]
    owner["private_read_tools"] = ["calendar_list_events", "image_search"]
    _write_policy(policy_path, policy)
    taint = tier_policy.guard_tier_middlewares([], [], tier_policy.policy_for("owner"))[0].taint
    assert taint.permitted_after_taint("gmail_create_draft")  # in write_tools and self_write_tools
    assert not taint.permitted_after_taint("github_create_issue")  # write_tools only
    assert not taint.permitted_after_taint("web_fetch")  # egress beats self_write_tools
    assert not taint.permitted_after_taint("image_search")  # egress beats private_read_tools
    assert taint.permitted_after_taint("calendar_list_events")
    # A self-write that is missing from write_tools still works, with a warning.
    assert taint.permitted_after_taint("slack_post_draft")
    assert "self_write_tools not in write_tools: slack_post_draft" in caplog.text


def test_a_draft_after_a_private_read_runs_and_an_issue_does_not(owner_policy):
    guard = _guard(owner_policy)
    result, executed = _call(guard, "gmail_create_draft", _after_calendar("gmail_create_draft"))
    assert executed == ["gmail_create_draft"]
    guard.allowed_names = guard.allowed_names | {"github_create_issue"}
    result, executed = _call(guard, "github_create_issue", _after_calendar("github_create_issue"))
    assert executed == [] and result.content == tier_policy.TAINT_REFUSAL


def test_labels_are_short_and_header_safe():
    assert tier_policy.source_label("Google Calendar!") == "google-calendar"
    assert tier_policy.source_label("x" * 80) == "x" * 32
    assert tier_policy.turn_private_headers(["gmail", "calendar", "gmail"]) == {"X-Argus-Turn-Private": "1", "X-Argus-Turn-Sources": "calendar,gmail"}
    assert tier_policy._DYNAMIC_CONTEXT_REMINDER_KEY == __import__("deerflow.agents.middlewares.dynamic_context_middleware", fromlist=["_"])._DYNAMIC_CONTEXT_REMINDER_KEY


# -- the taint guard -----------------------------------------------------------

GUARD_TOOLS = [
    NamedTool("web_search"),
    NamedTool("web_fetch"),
    NamedTool("bash"),
    NamedTool("read_secret_notes"),
    NamedTool("present_files"),
    _mcp("pythia_query", "pythia"),
    _mcp("asana_create_task", "asana"),
    _mcp("calendar_list_events", "calendar"),
    _mcp("gmail_search", "gmail"),
    _mcp("gmail_create_draft", "gmail"),
    _mcp("gmail_send_message", "gmail"),
]


def _guard(policy):
    stack = tier_policy.guard_tier_middlewares([], GUARD_TOOLS, policy)
    guard = stack[0]
    assert isinstance(guard, TierToolGuardMiddleware) and guard.taint is not None
    return guard


def _ai(*names, msg_id):
    return AIMessage(content="", id=msg_id, tool_calls=[{"name": name, "args": {}, "id": f"{msg_id}-{i}"} for i, name in enumerate(names)])


def _call(guard, name, messages, *, pre_existing=frozenset({"h0", "a0"})):
    """Run the guard for the newest AI message's call of ``name`` (its own id, as the tool node does)."""
    runtime = SimpleNamespace(context={CURRENT_RUN_PRE_EXISTING_MESSAGE_IDS_KEY: pre_existing})
    last_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
    call_id = next((c["id"] for c in (last_ai.tool_calls if last_ai else []) if c["name"] == name), "call-x")
    request = SimpleNamespace(tool_call={"name": name, "id": call_id, "args": {}}, state={"messages": messages}, runtime=runtime)
    executed = []

    def handler(req):
        executed.append(req.tool_call["name"])
        return ToolMessage(content="ok", tool_call_id=call_id)

    result = guard.wrap_tool_call(request, handler)
    return result, executed


def _turn(*messages):
    # A previous turn that read gmail, then this run's user message.
    return [HumanMessage(content="earlier", id="h0"), _ai("gmail_search", msg_id="a0"), HumanMessage(content="now", id="h1"), *messages]


def _after_calendar(*names):
    """This run read the calendar, then asks for ``names``."""
    return _turn(_ai("calendar_list_events", msg_id="a1"), ToolMessage(content="9:00 board", tool_call_id="a1-0"), _ai(*names, msg_id="a2"))


def test_before_taint_everything_in_the_ceiling_runs(owner_policy):
    guard = _guard(owner_policy)
    messages = _turn(_ai("web_search", "pythia_query", "asana_create_task", "gmail_create_draft", msg_id="a1"))
    for name in ("web_search", "pythia_query", "asana_create_task", "gmail_create_draft"):
        result, executed = _call(guard, name, messages)
        assert executed == [name] and result.content == "ok", name
    # The first PRIVATE call itself runs under the full ceiling, allowlisted or not.
    for name in ("bash", "web_fetch", "gmail_send_message", "present_files", "calendar_list_events"):
        result, executed = _call(guard, name, _turn(_ai(name, msg_id="a1")))
        assert executed == [name], name


def test_after_taint_only_the_allowlist_runs(owner_policy):
    guard = _guard(owner_policy)
    # Allowed: a private read, a self-write, a PUBLIC non-egress tool.
    for name in ("gmail_search", "gmail_create_draft", "pythia_query"):
        result, executed = _call(guard, name, _after_calendar(name))
        assert executed == [name], name
    # Refused: a write, egress (web_search is also PUBLIC), bash, a send, an unclassified tool, present_files.
    for name in ("asana_create_task", "web_search", "web_fetch", "bash", "gmail_send_message", "read_secret_notes", "present_files"):
        result, executed = _call(guard, name, _after_calendar(name))
        assert executed == [] and result.status == "error", name
        assert result.content == tier_policy.TAINT_REFUSAL


def test_a_private_call_in_the_same_step_counts(owner_policy):
    guard = _guard(owner_policy)
    messages = _turn(_ai("calendar_list_events", "asana_create_task", "bash", msg_id="a1"))
    # The read runs (it is allowlisted); the write and the second PRIVATE call are after taint.
    assert _call(guard, "calendar_list_events", messages)[1] == ["calendar_list_events"]
    assert _call(guard, "asana_create_task", messages)[0].content == tier_policy.TAINT_REFUSAL
    assert _call(guard, "bash", messages)[0].content == tier_policy.TAINT_REFUSAL


def test_an_unknown_tool_is_private_and_taints(owner_policy):
    guard = _guard(owner_policy)
    messages = _turn(_ai("read_secret_notes", msg_id="a1"), _ai("asana_create_task", msg_id="a2"))
    result, _ = _call(guard, "asana_create_task", messages)
    assert result.content == tier_policy.TAINT_REFUSAL


def test_an_earlier_turn_does_not_taint_this_run(owner_policy):
    guard = _guard(owner_policy)
    # a0 (gmail_search) belongs to a previous run: pre-existing ids mark the boundary.
    result, executed = _call(guard, "asana_create_task", _turn(_ai("asana_create_task", msg_id="a1")))
    assert executed == ["asana_create_task"]
    # Without the worker's boundary, the last visible human message is used.
    result, executed = _call(guard, "asana_create_task", _turn(_ai("asana_create_task", msg_id="a1")), pre_existing=None)
    assert executed == ["asana_create_task"]


def test_injected_memory_is_a_private_read(owner_policy):
    guard = _guard(owner_policy)
    memory = HumanMessage(content="Nicholas prefers...", id="h1__memory", additional_kwargs={"hide_from_ui": True, "dynamic_context_reminder": True})
    messages = [memory, HumanMessage(content="now", id="h1__user"), _ai("asana_create_task", msg_id="a1")]
    result, executed = _call(guard, "asana_create_task", messages, pre_existing=frozenset())
    assert executed == [] and result.content == tier_policy.TAINT_REFUSAL
    assert guard._run_sources({"messages": messages}, SimpleNamespace(context={})) == ["memory"]


def test_tool_calls_outside_the_build_are_still_refused(owner_policy):
    guard = _guard(owner_policy)
    result, executed = _call(guard, "task", _turn(_ai("task", msg_id="a1")))
    assert executed == [] and "not available" in result.content


def test_a_tainted_run_stamps_its_answer(owner_policy):
    guard = _guard(owner_policy)
    runtime = SimpleNamespace(context={CURRENT_RUN_PRE_EXISTING_MESSAGE_IDS_KEY: frozenset({"h0", "a0"})})
    answer = AIMessage(content="You have the board at 9.", id="a2")
    state = {"messages": _turn(_ai("calendar_list_events", "gmail_search", msg_id="a1"), answer)}
    update = guard.after_model(state, runtime)
    (stamped,) = update["messages"]
    assert stamped.id == "a2" and stamped.additional_kwargs[tier_policy.TURN_PRIVATE_KEY] == ["calendar", "gmail"]
    assert guard.after_agent({"messages": [*state["messages"][:-1], stamped]}, runtime) is None  # already stamped
    # An untainted run (PUBLIC and WRITE calls only) is not stamped.
    clean = {"messages": _turn(_ai("web_search", "gmail_create_draft", msg_id="a1"), AIMessage(content="Drafted.", id="a2"))}
    assert guard.after_model(clean, runtime) is None


def test_the_async_guard_refuses_the_same(owner_policy):
    guard = _guard(owner_policy)
    runtime = SimpleNamespace(context={CURRENT_RUN_PRE_EXISTING_MESSAGE_IDS_KEY: frozenset({"h0", "a0"})})
    request = SimpleNamespace(tool_call={"name": "web_search", "id": "a1-1", "args": {}}, state={"messages": _turn(_ai("calendar_list_events", "web_search", msg_id="a1"))}, runtime=runtime)

    async def handler(req):
        raise AssertionError("must not run")

    result = asyncio.run(guard.awrap_tool_call(request, handler))
    assert result.content == tier_policy.TAINT_REFUSAL


def test_the_manager_reads_the_stamp():
    result = {
        "messages": [
            {"type": "human", "content": "what's on today"},
            {"type": "ai", "content": "", "tool_calls": [{"name": "calendar_list_events"}], "additional_kwargs": {tier_policy.TURN_PRIVATE_KEY: ["calendar"]}},
            {"type": "tool", "content": "9:00 board"},
            {"type": "ai", "content": "The board at 9.", "additional_kwargs": {tier_policy.TURN_PRIVATE_KEY: ["calendar", "Gmail Inbox"]}},
        ]
    }
    from app.channels.manager import _current_turn_messages

    assert tier_policy.turn_private_labels(_current_turn_messages(result)) == ["calendar", "gmail-inbox"]
    assert tier_policy.turn_private_labels([{"type": "ai", "content": "hi"}]) == []


# -- assembly -----------------------------------------------------------------------


AGENT = AgentConfig(name="atlas", memory_enabled=True, pythia_ring="personal")


def _assemble(monkeypatch, context, tools, *, agent=AGENT, memory_tools=True):
    from deerflow.agents.lead_agent import agent as lead_agent_module

    captured = {}

    def build_middlewares(*args, **kwargs):
        captured["build_kwargs"] = kwargs
        return []

    def append_memory(candidates):
        candidates.append(NamedTool("memory_store"))

    monkeypatch.setattr(lead_agent_module, "_resolve_model_name", lambda x=None, **kwargs: "default-model")
    monkeypatch.setattr(lead_agent_module, "create_chat_model", lambda **kwargs: "model")
    monkeypatch.setattr(lead_agent_module, "build_middlewares", build_middlewares)
    monkeypatch.setattr(lead_agent_module, "apply_prompt_template", lambda **kwargs: "BASE PROMPT")
    monkeypatch.setattr(lead_agent_module, "create_agent", lambda **kwargs: kwargs)
    monkeypatch.setattr(lead_agent_module, "load_agent_config", lambda name, *, user_id=None: agent.model_copy(update={"name": name}))
    monkeypatch.setattr(lead_agent_module, "_load_enabled_available_skills", lambda available_skills, *, app_config, user_id=None: [])
    monkeypatch.setattr(lead_agent_module, "should_use_memory_tools", lambda memory: memory_tools)
    monkeypatch.setattr(lead_agent_module, "_append_memory_tools_without_name_conflicts", append_memory)
    monkeypatch.setattr("deerflow.tools.get_available_tools", lambda **kwargs: list(tools))

    mock_app_config = MagicMock()
    mock_app_config.database.checkpoint_channel_mode = "full"
    mock_app_config.database.checkpoint_delta.snapshot_frequency = 10
    mock_app_config.get_model_config.return_value = SimpleNamespace(supports_thinking=False, supports_vision=False)
    mock_app_config.tool_search.enabled = True
    mock_app_config.tool_search.auto_promote_top_k = 3
    mock_app_config.tool_search.exclude = []
    mock_app_config.tool_search.defer = []
    mock_app_config.skills.container_path = "/mnt/skills"
    mock_app_config.skills.deferred_discovery = False
    monkeypatch.setattr(lead_agent_module, "get_app_config", lambda: mock_app_config)

    config = {"configurable": {}, "context": dict(context)}
    captured["agent_kwargs"] = lead_agent_module.make_lead_agent(config)
    captured["config"] = config
    return captured


OWNER_TOOLS = [
    NamedTool("bash"),
    NamedTool("task"),
    NamedTool("web_search"),
    NamedTool("slack_channel_history"),
    _mcp("pythia_query", "pythia"),
    _mcp("calendar_list_events", "calendar"),
    _mcp("asana_create_task", "asana"),
]


def test_owner_assembly_keeps_the_owners_agent_and_tools_minus_subagents(owner_policy, monkeypatch):
    captured = _assemble(monkeypatch, {"argus_tier": "owner", "agent_name": "atlas", "subagent_enabled": True}, OWNER_TOOLS)
    kwargs = captured["agent_kwargs"]
    names = {t.name for t in kwargs["tools"]}
    # The owner's tools stay, channel-thread tools included; subagents, update_agent and memory tools go.
    assert {"bash", "web_search", "slack_channel_history", "pythia_query", "calendar_list_events", "asana_create_task"} <= names
    assert not names & {"task", "update_agent", "memory_store"}
    # Deferral stays on for the owner's MCP catalog.
    assert "tool_search" in names
    assert captured["build_kwargs"]["agent_name"] == "atlas"
    # Memory is read (injection), never written: no memory tools, memory_mode stays read-only from admission.
    assert captured["build_kwargs"]["memory_enabled"] is True
    assert captured["config"]["configurable"]["subagent_enabled"] is False
    guard = kwargs["middleware"][0]
    assert isinstance(guard, TierToolGuardMiddleware) and guard.taint is not None
    assert guard.taint.of("calendar_list_events").kind == "private"
    assert guard.taint.of("pythia_query").kind == "public"
    assert guard.taint.of("asana_create_task").kind == "write"
    assert guard.taint.of("tool_search").kind == "public"  # listed in public_tools
    assert guard.taint.of("bash").kind == "private"  # not listed: default deny


def test_owner_memory_off_reads_nothing(policy_path, agents_present, monkeypatch):
    policy = copy.deepcopy(POLICY)
    policy["tiers"]["owner"]["memory_mode"] = "off"
    _write_policy(policy_path, policy)
    captured = _assemble(monkeypatch, {"argus_tier": "owner", "agent_name": "atlas"}, OWNER_TOOLS)
    assert captured["build_kwargs"]["memory_enabled"] is False


@pytest.mark.asyncio
async def test_memory_off_injects_nothing_and_writes_nothing(policy_path, agents_present):
    from deerflow.agents.lead_agent import agent as lead_agent_module
    from deerflow.agents.memory.write_policy import effective_memory_mode
    from deerflow.agents.middlewares.dynamic_context_middleware import DynamicContextMiddleware
    from deerflow.agents.middlewares.memory_middleware import MemoryMiddleware
    from deerflow.config.app_config import AppConfig

    policy = copy.deepcopy(POLICY)
    policy["tiers"]["owner"]["memory_mode"] = "off"
    _write_policy(policy_path, policy)
    owner = tier_policy.policy_for("owner")
    assert owner.memory_mode == "off" and not owner.reads_memory
    # Admission: the run's memory mode is off, so nothing is written and the injector reads nothing.
    store = MemoryThreadMetaStore(InMemoryStore())
    await store.create("thread-c", user_id="owner-1")
    config = {"configurable": {"memory_mode": "read-write"}, "context": {"argus_tier": "owner", "agent_name": "atlas", "memory_mode": "read-write"}}
    await tier_policy.admit_run_tier(config, store, "thread-c", {})
    runtime = SimpleNamespace(context=config["context"])
    assert effective_memory_mode(runtime) == "off"
    assert memory_write_allowed(runtime) is False
    # Assembly passes memory_enabled=False (test_owner_memory_off_reads_nothing), and the real
    # middleware chain built that way has no memory writer and an injector with memory switched off.
    app_config = AppConfig.model_validate({"sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"}})
    stack = lead_agent_module.build_middlewares({"configurable": {}, "context": {"argus_tier": "owner"}}, "default-model", agent_name="atlas", app_config=app_config, memory_enabled=owner.reads_memory)
    assert not any(isinstance(m, MemoryMiddleware) for m in stack)
    (injector,) = [m for m in stack if isinstance(m, DynamicContextMiddleware)]
    assert injector._memory_enabled is False


def test_the_pythia_ring_is_capped_at_internal(owner_policy):
    assert tier_policy.clamp_pythia_ring("personal", owner_policy) == "internal"
    assert tier_policy.clamp_pythia_ring("hierarchical", owner_policy) == "internal"
    assert tier_policy.clamp_pythia_ring("internal", owner_policy) == "internal"


def test_the_owner_build_drops_the_retrieval_ring_to_internal(owner_policy, monkeypatch):
    from deerflow.agents.lead_agent import agent as lead_agent_module
    from deerflow.config.app_config import AppConfig

    rings = []

    class Retrieval:
        def __init__(self, ring):
            rings.append(ring)

    monkeypatch.setattr("deerflow.agents.middlewares.pythia_retrieval_middleware.PythiaRetrievalMiddleware", Retrieval)
    monkeypatch.setattr(lead_agent_module, "load_agent_config", lambda name, *, user_id=None: AGENT)
    monkeypatch.delenv("PYTHIA_ROUTER_INJECT", raising=False)
    monkeypatch.delenv("PYTHIA_RETRIEVAL_ENABLED", raising=False)
    app_config = AppConfig.model_validate({"sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"}})
    # The agent asks for its personal ring; the owner tier attaches retrieval at internal.
    lead_agent_module.build_middlewares({"configurable": {}, "context": {"argus_tier": "owner"}}, "default-model", agent_name="atlas", app_config=app_config)
    lead_agent_module.build_middlewares({"configurable": {}, "context": {}}, "default-model", agent_name="atlas", app_config=app_config)
    assert rings == ["internal", "personal"]


# -- run admission -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_owner_admission_reads_memory_without_writing_and_binds_nothing(owner_policy):
    store = MemoryThreadMetaStore(InMemoryStore())
    await store.create("thread-c", user_id="owner-1")
    config = {"configurable": {"memory_mode": "read-write"}, "context": {"argus_tier": "owner", "agent_name": "atlas", "user_email": "owner@example.com", "memory_mode": "read-write"}}
    run_metadata = {}
    policy = await tier_policy.admit_run_tier(config, store, "thread-c", run_metadata)
    assert policy.tier == "owner"
    context = config["context"]
    assert context["agent_name"] == "atlas"  # the owner's own agent
    assert context["memory_mode"] == "read-only" and config["configurable"]["memory_mode"] == "read-only"
    assert "user_email" not in context
    # Memory is not written in an owner turn (MemoryMiddleware and the summary flush both ask this).
    assert memory_write_allowed(SimpleNamespace(context=context)) is False
    # The owner tier is not bound to the thread: a later web run there is the owner's own.
    assert "argus_tier" not in (await store.get("thread-c", user_id=None))["metadata"]
    assert "argus_tier" not in run_metadata


@pytest.mark.asyncio
async def test_a_public_thread_stays_public_for_an_owner_run(owner_policy):
    store = MemoryThreadMetaStore(InMemoryStore())
    await store.create("thread-p", user_id="owner-1", metadata={"argus_tier": "public"})
    config = {"configurable": {}, "context": {"argus_tier": "owner", "agent_name": "atlas"}}
    policy = await tier_policy.admit_run_tier(config, store, "thread-p", {})
    assert policy.tier == "public" and config["context"]["agent_name"] == "atlas-slack-public"


# -- the MCP interceptor ---------------------------------------------------------


def _extensions():
    return ExtensionsConfig(
        mcp_servers={name: McpServerConfig(enabled=True, type="http", url=url, headers={"X-Kb-Api-Key": "k"}) for name, url in GLOBAL_MCP.items()},
        skills={},
    )


def _mcp_request(server_name, tier, headers=None):
    from langchain_mcp_adapters.interceptors import MCPToolCallRequest

    return MCPToolCallRequest(name="query", args={}, server_name=server_name, headers=headers, runtime=SimpleNamespace(context={"argus_tier": tier} if tier else {}))


def test_owner_mcp_calls_reach_every_server_with_the_owner_header(owner_policy):
    from deerflow.mcp.run_tier import build_run_tier_interceptor

    interceptor = build_run_tier_interceptor(_extensions())
    seen = {}

    async def transport(request):
        seen[request.server_name] = dict(request.headers or {})
        return "result"

    minted = {"X-Pythia-Caller-Token": "signed-for-owner"}
    for server in ("gmail", "calendar", "db", "pythia"):
        assert asyncio.run(interceptor(_mcp_request(server, "owner", headers=dict(minted)), transport)) == "result"
    assert {headers["X-Argus-Run-Tier"] for headers in seen.values()} == {"owner"}
    # Pythia (a listed server) is capped at the internal ring: no caller token.
    assert "X-Pythia-Caller-Token" not in seen["pythia"]
    # db/bq keep the owner's identity.
    assert seen["db"]["X-Pythia-Caller-Token"] == "signed-for-owner"


def test_an_untiered_run_is_still_the_plain_owner(owner_policy):
    from deerflow.mcp.run_tier import build_run_tier_interceptor

    interceptor = build_run_tier_interceptor(_extensions())
    seen = []

    async def transport(request):
        seen.append(dict(request.headers or {}))
        return "result"

    asyncio.run(interceptor(_mcp_request("pythia", None, headers={"X-Pythia-Caller-Token": "t"}), transport))
    assert seen[0]["X-Argus-Run-Tier"] == "owner" and seen[0]["X-Pythia-Caller-Token"] == "t"


# -- the relay and the probe -------------------------------------------------------


class RouterClient:
    """A relay WebClient double that remembers how it was built (headers) and what it posted."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls: list[tuple[str, dict]] = []

    def auth_test(self, **kwargs):
        return {"ok": True, "user_id": "UATLAS"}

    def chat_postMessage(self, **kwargs):
        self.calls.append(("chat.postMessage", kwargs))
        return {"ok": True, "ts": f"ts.{len(self.calls)}"}

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
        return {"ok": True, "messages": []}


class Factory:
    def __init__(self):
        self.clients: list[RouterClient] = []

    def __call__(self, **kwargs):
        client = RouterClient(**kwargs)
        self.clients.append(client)
        return client


def _relay_config(**overrides):
    config = {
        "transport": "relay",
        "relay_url": "http://slack-router:8080/api/",
        "relay_token": "router-key",
        "tier_agents": {"dm": "atlas"},
        "session": {"context": {"agent_name": "atlas"}},
        "web_client_factory": Factory(),
    }
    config.update(overrides)
    return config


async def _started(config=None, bus=None):
    channel = SlackChannel(bus=bus or MessageBus(), config=config or _relay_config())
    await channel.start()
    return channel


def _event(**overrides):
    event = {"type": "app_mention", "channel": "C1", "user": "U-owner", "text": "<@UATLAS> make a task for the board prep", "ts": "2.0", "thread_ts": "1.0"}
    event.update(overrides)
    return event


def _argus(tier="owner"):
    return {"tier": tier, "action": "message", "trigger_user": "U-owner", "persona": "Atlas for Nicholas", "channel_label": "#eng"}


@pytest.mark.asyncio
@pytest.mark.parametrize(("tier_agents", "agent"), [({"dm": "atlas"}, None), ({"dm": "atlas", "owner": "atlas-channel"}, "atlas-channel")])
async def test_an_owner_event_runs_the_owners_agent(owner_policy, tier_agents, agent):
    bus = MessageBus()
    channel = await _started(_relay_config(tier_agents=tier_agents), bus=bus)
    try:
        assert await channel.relay_event({"team_id": "T1", "event": _event()}, _argus()) is True
        inbound = await asyncio.wait_for(bus.get_inbound(), 2)
        # A command stays chat text in a channel.
        assert await channel.relay_event({"team_id": "T1", "event": _event(text="/memory", ts="3.0")}, _argus()) is True
        command = await asyncio.wait_for(bus.get_inbound(), 2)
    finally:
        await channel.stop()
    # None: no per-message override, so the channel default (session agent_name) runs.
    assert inbound.agent_name == agent
    assert inbound.metadata["argus_tier"] == "owner"
    assert command.msg_type.value == "chat"


@pytest.mark.asyncio
async def test_an_owner_event_is_refused_without_a_policy_or_its_agent(policy_path, agents_present):
    no_owner = copy.deepcopy(POLICY)
    del no_owner["tiers"]["owner"]
    _write_policy(policy_path, no_owner)
    channel = await _started()
    try:
        assert await channel.relay_event({"team_id": "T1", "event": _event()}, _argus()) is False
        _write_policy(policy_path, POLICY)
        agents_present.discard("atlas")  # the session's default agent
        assert await channel.relay_event({"team_id": "T1", "event": _event(ts="4.0")}, _argus()) is False
    finally:
        await channel.stop()


TOKEN = "test-internal-token"


@pytest.fixture()
def probe_client():
    from app.gateway.routers.channels import router

    app = FastAPI()
    app.include_router(router)
    with patch.object(internal_auth, "_INTERNAL_AUTH_TOKEN", TOKEN), TestClient(app, raise_server_exceptions=False) as client:
        yield client


async def _probe(probe_client, channel):
    service = MagicMock()
    service.get_channel.return_value = channel
    with patch("app.channels.service.get_channel_service", return_value=service):
        resp = await asyncio.to_thread(probe_client.get, "/api/channels/slack/relay/capabilities", headers={internal_auth.INTERNAL_AUTH_HEADER_NAME: TOKEN})
    assert resp.status_code == 200
    return resp.json()


@pytest.mark.asyncio
async def test_the_probe_lists_owner_when_its_policy_and_agent_resolve(policy_path, agents_present, probe_client):
    channel = await _started(_relay_config(tier_agents={"dm": "atlas", "public": "atlas-slack-public"}))
    try:
        _write_policy(policy_path, POLICY)
        both = await _probe(probe_client, channel)
        agents_present.discard("atlas")
        no_agent = await _probe(probe_client, channel)
        agents_present.add("atlas")
        no_owner = copy.deepcopy(POLICY)
        del no_owner["tiers"]["owner"]
        _write_policy(policy_path, no_owner)
        without = await _probe(probe_client, channel)
    finally:
        await channel.stop()
    assert both["tiers"] == ["dm", "public", "owner"]
    assert "owner" not in no_agent["tiers"]
    assert without["tiers"] == ["dm", "public"]


# -- the tainted answer reaches the router flagged ---------------------------------


@pytest.mark.asyncio
async def test_only_a_tainted_answer_and_its_files_carry_the_headers(owner_policy, tmp_path):
    channel = await _started()
    factory = channel.config["web_client_factory"]
    operator = channel._web_client
    attachment = tmp_path / "agenda.md"
    attachment.write_text("board prep")
    try:
        tainted = OutboundMessage(channel_name="slack", chat_id="C1", thread_id="t1", text="Board at 9.", thread_ts="1.0", metadata={tier_policy.TURN_PRIVATE_KEY: ["calendar", "gmail"]})
        await channel.send(tainted)
        await channel.send_file(tainted, ResolvedAttachment(virtual_path="/mnt/user-data/outputs/agenda.md", actual_path=attachment, filename="agenda.md", mime_type="text/markdown", size=10, is_image=False))
        clean = OutboundMessage(channel_name="slack", chat_id="C1", thread_id="t1", text="Task created.", thread_ts="1.0", metadata={})
        await channel.send(clean)
    finally:
        await channel.stop()
    flagged = [client for client in factory.clients if "headers" in client.kwargs]
    assert len(flagged) == 2  # one for the answer, one for its file
    for client in flagged:
        assert client.kwargs["headers"] == {"X-Argus-Turn-Private": "1", "X-Argus-Turn-Sources": "calendar,gmail"}
        assert client.kwargs["base_url"] == "http://slack-router:8080/api/" and client.kwargs["token"] == "router-key"
    assert [name for name, _ in flagged[0].calls] == ["chat.postMessage"]
    assert [name for name, _ in flagged[1].calls] == ["files.upload_v2"]
    # Reactions, the ack cleanup and the untainted answer go through the operator client, unflagged.
    operator_calls = [name for name, _ in operator.calls]
    assert "reactions.add" in operator_calls
    assert [kwargs["text"] for name, kwargs in operator.calls if name == "chat.postMessage"] == ["Task created."]


@pytest.mark.asyncio
async def test_every_post_and_upload_of_a_tainted_turn_is_flagged_and_none_of_a_clean_one(owner_policy, tmp_path):
    """The channel's own outbound dispatch: the answer, then each attachment (what present_files produces)."""
    channel = await _started()
    factory = channel.config["web_client_factory"]
    operator = channel._web_client
    files = []
    for name in ("agenda.md", "notes.pdf"):
        path = tmp_path / name
        path.write_text(name)
        files.append(ResolvedAttachment(virtual_path=f"/mnt/user-data/outputs/{name}", actual_path=path, filename=name, mime_type="text/plain", size=len(name), is_image=False))
    try:
        await channel._on_outbound(OutboundMessage(channel_name="slack", chat_id="C1", thread_id="t1", text="Board at 9.", thread_ts="1.0", attachments=files, metadata={tier_policy.TURN_PRIVATE_KEY: ["calendar"]}))
        tainted_clients = [client for client in factory.clients if "headers" in client.kwargs]
        await channel._on_outbound(OutboundMessage(channel_name="slack", chat_id="C1", thread_id="t2", text="Task created.", thread_ts="5.0", attachments=files, metadata={}))
    finally:
        await channel.stop()
    calls = [name for client in tainted_clients for name, _ in client.calls]
    assert sorted(calls) == ["chat.postMessage", "files.upload_v2", "files.upload_v2"]
    assert all(client.kwargs["headers"][tier_policy.TURN_PRIVATE_HEADER] == "1" for client in tainted_clients)
    # The clean turn made no flagged client; its post and uploads went through the operator client.
    assert [client for client in factory.clients if "headers" in client.kwargs] == tainted_clients
    assert [name for name, kwargs in operator.calls if name in ("chat.postMessage", "files.upload_v2") and kwargs.get("thread_ts") == "5.0"] == ["chat.postMessage", "files.upload_v2", "files.upload_v2"]


@pytest.mark.asyncio
async def test_a_tainted_owner_run_posts_its_answer_flagged_end_to_end(owner_policy, tmp_path):
    from app.channels.manager import ChannelManager

    bus = MessageBus()
    store = JsonChannelStore(path=tmp_path / "store.json")
    channel = await _started(_relay_config(channel_store=store), bus=bus)
    factory = channel.config["web_client_factory"]
    service = MagicMock()
    service.get_channel.side_effect = {"slack": channel}.get
    manager = ChannelManager(bus=bus, store=store, channel_sessions={"slack": {"context": {"agent_name": "atlas", "user_email": "owner@example.com"}}})
    lg = MagicMock()
    lg.threads.create = AsyncMock(return_value={"thread_id": "thread-c"})
    lg.threads.get = AsyncMock(return_value={"thread_id": "thread-c", "metadata": {}})
    lg.runs.wait = AsyncMock(
        return_value={
            "messages": [
                {"type": "human", "content": "what's my day"},
                {"type": "ai", "content": "", "tool_calls": [{"name": "calendar_list_events", "args": {}, "id": "c1"}], "additional_kwargs": {tier_policy.TURN_PRIVATE_KEY: ["calendar"]}},
                {"type": "tool", "content": "9:00 board", "tool_call_id": "c1"},
                {"type": "ai", "content": "The board at 9.", "additional_kwargs": {tier_policy.TURN_PRIVATE_KEY: ["calendar"]}},
            ]
        }
    )
    manager._client = lg
    try:
        with patch("app.channels.service.get_channel_service", return_value=service):
            await manager.start()
            assert await channel.relay_event({"team_id": "T1", "event": _event()}, _argus()) is True
            async with asyncio.timeout(5):
                while not any("headers" in client.kwargs and client.calls for client in factory.clients):
                    await asyncio.sleep(0.02)
    finally:
        await manager.stop()
        await channel.stop()
    run_kwargs = lg.runs.wait.await_args.kwargs
    assert run_kwargs["context"]["argus_tier"] == "owner"
    assert run_kwargs["context"]["memory_mode"] == "read-only"
    assert "user_email" not in run_kwargs["context"]
    (flagged,) = [client for client in factory.clients if "headers" in client.kwargs]
    assert flagged.kwargs["headers"] == {"X-Argus-Turn-Private": "1", "X-Argus-Turn-Sources": "calendar"}
    assert [kwargs["text"] for _, kwargs in flagged.calls] == ["The board at 9."]
    # The progress ("Working on it") message went out unflagged on the operator client.
    acks = [kwargs for name, kwargs in channel._web_client.calls if name == "chat.postMessage"]
    assert acks and all("board" not in kwargs["text"] for kwargs in acks)


@pytest.mark.asyncio
async def test_the_manager_sends_its_resolved_owner_context(owner_policy, tmp_path):
    from app.channels.manager import ChannelManager

    manager = ChannelManager(bus=MessageBus(), store=JsonChannelStore(path=tmp_path / "store.json"), channel_sessions={"slack": {"context": {"agent_name": "atlas", "user_email": "owner@example.com", "memory_mode": "read-write"}}})
    msg = InboundMessage(channel_name="slack", chat_id="C1", user_id="U-owner", text="hi", thread_ts="1.0", metadata={"argus_tier": "owner", "persona": "Atlas for Nicholas"})
    _, run_config, run_context = manager._resolve_run_params(msg, "thread-c")
    assert run_context["argus_tier"] == "owner"
    assert run_context["agent_name"] == "atlas"
    assert run_context["memory_mode"] == "read-only"
    assert "user_email" not in run_context
    assert run_context["argus_tier_vars"] == {"persona": "Atlas for Nicholas"}


# -- the guard inside a real agent graph ------------------------------------------


@pytest.mark.parametrize("invocation", ["invoke", "ainvoke"])
def test_a_real_graph_refuses_the_write_after_a_private_read_and_stamps_the_answer(owner_policy, invocation):
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.tools import StructuredTool

    class Model(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    executed = []

    def calendar_list_events() -> str:
        """Read the owner's calendar."""
        executed.append("calendar")
        return "9:00 board"

    def asana_create_task(name: str = "") -> str:
        """Create an Asana task."""
        executed.append("asana")
        return "created"

    calendar = tag_mcp_tool(StructuredTool.from_function(calendar_list_events), server_name="calendar", transport="http")
    asana = tag_mcp_tool(StructuredTool.from_function(asana_create_task), server_name="asana", transport="http")
    guard = tier_policy.guard_tier_middlewares([], [calendar, asana], owner_policy)[0]
    responses = iter(
        [
            AIMessage(content="", tool_calls=[{"name": "calendar_list_events", "args": {}, "id": "c1"}]),
            AIMessage(content="", tool_calls=[{"name": "asana_create_task", "args": {"name": "prep"}, "id": "c2"}]),
            AIMessage(content="The board is at 9; ask again to make the task."),
        ]
    )
    graph = create_agent(Model(messages=responses), tools=[calendar, asana], middleware=[guard])
    history = {"messages": [HumanMessage(content="earlier", id="h0"), HumanMessage(content="what's on, and make a prep task", id="h1")]}
    context = {"thread_id": "t", CURRENT_RUN_PRE_EXISTING_MESSAGE_IDS_KEY: frozenset({"h0", "h1"})}
    result = graph.invoke(history, context=context) if invocation == "invoke" else asyncio.run(graph.ainvoke(history, context=context))
    assert executed == ["calendar"]
    refused = next(m for m in result["messages"] if isinstance(m, ToolMessage) and m.tool_call_id == "c2")
    assert refused.content == tier_policy.TAINT_REFUSAL
    final = result["messages"][-1]
    assert isinstance(final, AIMessage) and final.additional_kwargs[tier_policy.TURN_PRIVATE_KEY] == ["calendar"]
    # Every AI message of the run after the read carries the stamp the manager reads.
    assert tier_policy.turn_private_labels(result["messages"]) == ["calendar"]

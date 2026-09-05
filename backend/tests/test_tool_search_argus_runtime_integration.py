"""End-to-end tool discovery, policy, extension telemetry, and run journal test.

The Argus extension is intentionally loaded from its sibling checkout.  This
keeps the harness boundary honest: the test uses the public extension API and
the real LangGraph middleware assembly, rather than calling telemetry helpers
directly.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from deerflow_extension_api import ExtensionData, TaskInfo, TaskOutcome
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from pydantic import Field

from deerflow.agents.lead_agent.agent import build_middlewares
from deerflow.agents.middlewares.skill_tool_policy_middleware import SkillToolPolicyMiddleware
from deerflow.agents.thread_state import ThreadState
from deerflow.config.app_config import AppConfig
from deerflow.config.sandbox_config import SandboxConfig
from deerflow.extensions.notify import notify_task_start, notify_task_stop
from deerflow.extensions.registry import ExtensionRegistry
from deerflow.runtime.events.store.memory import MemoryRunEventStore
from deerflow.runtime.journal import RunJournal
from deerflow.runtime.runs.worker import _build_runtime_context
from deerflow.runtime.secret_context import write_slash_skill_source_path
from deerflow.skills.types import Skill, SkillCategory
from deerflow.tools.builtins.tool_search import build_deferred_tool_setup
from deerflow.tools.mcp_metadata import tag_mcp_tool

_PLUGIN_SRC = Path(os.environ["ARGUS_EXTENSION_SOURCE"]) if os.environ.get("ARGUS_EXTENSION_SOURCE") else None


@tool
def runtime_allowed_calculator(expression: str) -> str:
    """Calculate a value for the runtime integration test."""
    return "4"


@tool
def runtime_denied_lookup(query: str) -> str:
    """A tool denied by the active skill policy."""
    return "secret"


@tool
def runtime_direct_bound(value: str) -> str:
    """A directly bound tool used to verify runtime visibility reporting."""
    return value


class _RecordingModel(GenericFakeChatModel):
    bound_tool_names: list[list[str]] = Field(default_factory=list)

    def __init__(self, responses: list[AIMessage]):
        super().__init__(messages=iter(responses))

    def bind_tools(self, tools: Any, **kwargs: Any):
        self.bound_tool_names.append([getattr(candidate, "name", "") for candidate in tools])
        return self


class _Storage:
    def __init__(self, skill: Skill):
        self.skill = skill

    def load_skills(self, *, enabled_only: bool = False) -> list[Skill]:
        return [self.skill]

    def get_container_root(self) -> str:
        return "/mnt/skills"


def _skill() -> Skill:
    directory = Path("/tmp/skills/public/runtime-integration")
    return Skill(
        name="runtime-integration",
        description="Runtime integration fixture",
        license="MIT",
        skill_dir=directory,
        skill_file=directory / "SKILL.md",
        relative_path=Path("runtime-integration"),
        category=SkillCategory.PUBLIC,
        allowed_tools=(runtime_allowed_calculator.name, runtime_direct_bound.name),
        enabled=True,
    )


@pytest.mark.asyncio
async def test_runtime_tool_search_policy_and_argus_journal(monkeypatch):
    """A real graph run filters discovery, executes only the allowed tool, and
    records physical schemas and call timing through the extension lifecycle.
    """
    if _PLUGIN_SRC is None or not _PLUGIN_SRC.is_dir():
        pytest.skip("set ARGUS_EXTENSION_SOURCE to run the cross-repository Argus extension integration")
    monkeypatch.syspath_prepend(str(_PLUGIN_SRC))
    from argus_deerflow_extension import install

    allowed = runtime_allowed_calculator
    denied = runtime_denied_lookup
    setup = build_deferred_tool_setup(
        [tag_mcp_tool(allowed), tag_mcp_tool(denied), runtime_direct_bound],
        enabled=True,
    )
    registry = ExtensionRegistry()
    with registry.attributed_to("argus:install"):
        install(registry, {"enabled": True})
    extensions = registry.build()

    app_config = AppConfig(sandbox=SandboxConfig(use="deerflow.sandbox.local:LocalSandboxProvider"))
    stack = build_middlewares(
        config={"configurable": {}},
        model_name="gpt-4o",
        app_config=app_config,
        deferred_setup=setup,
        extensions=extensions,
    )
    policy = next(item for item in stack if isinstance(item, SkillToolPolicyMiddleware))
    policy._storage = lambda: _Storage(_skill())

    model = _RecordingModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "tool_search",
                        "args": {"query": "select:runtime_allowed_calculator,runtime_denied_lookup,runtime_direct_bound"},
                        "id": "search-call",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": runtime_allowed_calculator.name,
                        "args": {"expression": "2 + 2"},
                        "id": "calc-call",
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    graph = create_agent(
        model=model,
        tools=[allowed, denied, runtime_direct_bound, setup.tool_search_tool],
        middleware=stack,
        state_schema=ThreadState,
    )

    event_store = MemoryRunEventStore()
    journal = RunJournal("run-runtime", "thread-runtime", event_store, flush_threshold=100)
    task_store = ExtensionData("run-runtime")
    runtime_context = _build_runtime_context(
        "thread-runtime",
        "run-runtime",
        None,
        app_config,
        task_store,
        extensions,
    )
    runtime_context["__run_journal"] = journal
    skill = _skill()
    write_slash_skill_source_path(
        runtime_context,
        skill.get_container_file_path(),
        owner_token=policy._slash_source_owner_token,
    )
    info = TaskInfo(task_id="run-runtime", run_id="run-runtime", thread_id="thread-runtime", kind="lead")

    await notify_task_start(extensions, task_store, info)
    try:
        result = graph.invoke(
            {"messages": [HumanMessage(content="use the allowed calculator")]},
            context=runtime_context,
        )
    finally:
        await notify_task_stop(extensions, task_store, info, TaskOutcome.COMPLETED)
        await journal.flush()

    assert model.bound_tool_names[0] == [runtime_direct_bound.name, "tool_search"]
    assert runtime_allowed_calculator.name in model.bound_tool_names[1]
    assert runtime_direct_bound.name in model.bound_tool_names[0]
    assert runtime_direct_bound.name in model.bound_tool_names[1]
    assert runtime_denied_lookup.name not in model.bound_tool_names[1]
    assert result["messages"][-1].content == "done"
    search_message = next(message for message in result["messages"] if getattr(message, "name", None) == "tool_search")
    assert runtime_direct_bound.name in search_message.content

    events = await event_store.list_events("thread-runtime", "run-runtime", event_types=["middleware:tool_usage"])
    assert len(events) == 1
    changes = events[0]["content"]["changes"]
    assert changes["tool_call_counts"] == {"runtime_allowed_calculator": 1, "tool_search": 1}
    assert changes["tool_calls"]
    assert all(call["duration_ms"] >= 0 for call in changes["tool_calls"])
    physical = [call for call in changes["model_calls"] if call["available"]]
    assert physical
    assert all(call["schema_fingerprint"] for call in physical)
    assert any("tool_search" in call["schema_names"] for call in physical)
    assert any(runtime_allowed_calculator.name in call["schema_names"] for call in physical)
    assert runtime_denied_lookup.name not in {name for call in physical for name in call["schema_names"]}

"""[argus patch #146] tool_search answers directly for tools the run already has.

``tool_search.answer_bound_directly`` (default false): a select for tools
that are all already promoted (keyword auto-promotion or an earlier search)
or directly bound returns "already available, call it directly" without a
schema dump or a promotion, and routing hints for auto-promoted tools stop
telling the model to search for them. 67 of 175 tool_search selects in the
week to 2026-10-19 asked for a tool that was already bound.
"""

from __future__ import annotations

import asyncio

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langchain_core.tools import tool as as_tool
from langchain_core.utils.function_calling import convert_to_openai_function
from pydantic import BaseModel, Field

from deerflow.agents.middlewares.deferred_tool_filter_middleware import DeferredToolFilterMiddleware
from deerflow.agents.thread_state import ThreadState
from deerflow.config.tool_search_config import ToolSearchConfig
from deerflow.tools.builtins.tool_search import (
    DeferredToolCatalog,
    build_deferred_tool_setup,
    build_tool_search_tool,
    get_mcp_routing_hints_prompt_section,
)
from deerflow.tools.mcp_metadata import tag_mcp_routing, tag_mcp_tool


@as_tool
def mcp_draft(to: str) -> str:
    "Create a draft."
    return to


@as_tool
def mcp_issue(number: int) -> str:
    "Get an issue."
    return str(number)


def _invoke(tool, query: str, state: dict | None = None):
    return tool.invoke({"type": "tool_call", "name": "tool_search", "args": {"query": query, "state": state or {}}, "id": "tc1"})


def test_off_by_default_and_same_model_visible_schema():
    assert ToolSearchConfig().answer_bound_directly is False
    catalog = DeferredToolCatalog((tag_mcp_tool(mcp_draft),))
    upstream = build_tool_search_tool(catalog)
    aware = build_tool_search_tool(catalog, answer_bound_directly=True)
    assert convert_to_openai_function(aware) == convert_to_openai_function(upstream)


def test_select_for_promoted_tool_is_answered_without_a_schema():
    catalog = DeferredToolCatalog((tag_mcp_tool(mcp_draft), tag_mcp_tool(mcp_issue)))
    ts = build_tool_search_tool(catalog, answer_bound_directly=True)
    out = _invoke(ts, "select:mcp_draft", {"promoted": {"catalog_hash": catalog.hash, "names": ["mcp_draft"]}})
    assert out.update["promoted"] == {"catalog_hash": catalog.hash, "names": []}
    content = out.update["messages"][0].content
    assert content.startswith("Already available in this run: `mcp_draft`. Call it directly")
    assert '"parameters"' not in content


def test_select_mixing_bound_and_deferred_tools_runs_the_upstream_search():
    catalog = DeferredToolCatalog((tag_mcp_tool(mcp_draft), tag_mcp_tool(mcp_issue)))
    ts = build_tool_search_tool(catalog, answer_bound_directly=True)
    out = _invoke(ts, "select:mcp_draft,mcp_issue", {"promoted": {"catalog_hash": catalog.hash, "names": ["mcp_draft"]}})
    assert out.update["promoted"]["names"] == ["mcp_draft", "mcp_issue"]
    assert '"mcp_issue"' in out.update["messages"][0].content


def test_promotions_from_another_catalog_do_not_count():
    catalog = DeferredToolCatalog((tag_mcp_tool(mcp_draft),))
    ts = build_tool_search_tool(catalog, answer_bound_directly=True)
    out = _invoke(ts, "select:mcp_draft", {"promoted": {"catalog_hash": "stale", "names": ["mcp_draft"]}})
    assert out.update["promoted"]["names"] == ["mcp_draft"]


def test_directly_bound_tool_gets_the_short_answer_with_the_policy_caveat():
    catalog = DeferredToolCatalog((tag_mcp_tool(mcp_draft),))
    ts = build_tool_search_tool(catalog, frozenset({"calendar_list_events"}), answer_bound_directly=True)
    content = _invoke(ts, "select:calendar_list_events").update["messages"][0].content
    assert content.startswith("Already available in this run: `calendar_list_events`.")
    assert "current policy does not allow that tool" in content


def test_keyword_search_is_unchanged():
    catalog = DeferredToolCatalog((tag_mcp_tool(mcp_draft),))
    ts = build_tool_search_tool(catalog, answer_bound_directly=True)
    out = _invoke(ts, "draft", {"promoted": {"catalog_hash": catalog.hash, "names": ["mcp_draft"]}})
    assert out.update["promoted"]["names"] == ["mcp_draft"]


def test_real_graph_second_select_for_a_promoted_tool_is_answered_directly():
    setup = build_deferred_tool_setup([tag_mcp_tool(mcp_draft), tag_mcp_tool(mcp_issue)], enabled=True, answer_bound_directly=True)
    calls = [
        AIMessage(content="", tool_calls=[{"name": "tool_search", "args": {"query": "select:mcp_draft"}, "id": "c1", "type": "tool_call"}]),
        AIMessage(content="", tool_calls=[{"name": "tool_search", "args": {"query": "select:mcp_draft"}, "id": "c2", "type": "tool_call"}]),
        AIMessage(content="done"),
    ]

    class Model(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    graph = create_agent(
        model=Model(messages=iter(calls)),
        tools=[mcp_draft, mcp_issue, setup.tool_search_tool],
        middleware=[DeferredToolFilterMiddleware(setup.deferred_names, setup.catalog_hash)],
        state_schema=ThreadState,
    )
    result = asyncio.run(graph.ainvoke({"messages": [HumanMessage(content="draft the reply")]}))
    replies = [m.content for m in result["messages"] if isinstance(m, ToolMessage)]
    assert '"mcp_draft"' in replies[0]
    assert replies[1].startswith("Already available in this run: `mcp_draft`")
    assert result["promoted"] == {"catalog_hash": setup.catalog_hash, "names": ["mcp_draft"]}


class _Args(BaseModel):
    query: str = Field(..., description="query")


def _routed(name: str) -> StructuredTool:
    async def _call(query: str) -> str:
        return query

    tool = tag_mcp_tool(StructuredTool(name=name, description="Gmail drafts", args_schema=_Args, coroutine=_call))
    tag_mcp_routing(tool, {"mode": "prefer", "priority": 1, "keywords": ["draft", "email"]})
    return tool


def test_routing_hint_for_auto_promoted_tools_does_not_send_the_model_to_tool_search():
    tools = [_routed("gmail_create_draft")]
    upstream = get_mcp_routing_hints_prompt_section(tools, deferred_names=frozenset({"gmail_create_draft"}))
    assert "use `tool_search` to fetch `gmail_create_draft`" in upstream
    aware = get_mcp_routing_hints_prompt_section(tools, deferred_names=frozenset({"gmail_create_draft"}), deferred_auto_promoted=True)
    assert "use `tool_search` to fetch" not in aware
    assert "prefer the `gmail_create_draft` tool; it is loaded automatically for such requests" in aware

"""[argus patch #131] A playbook run's allowed-tools list is bound, not deferred."""

from __future__ import annotations

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from deerflow.agents.lead_agent.agent import deferral_exclude
from deerflow.config.tool_search_config import ToolSearchConfig
from deerflow.tools.builtins.tool_search import assemble_deferred_tools
from deerflow.tools.mcp_metadata import tag_mcp_tool


class _Args(BaseModel):
    sql: str = Field(description="SQL")


def _mcp(name: str) -> StructuredTool:
    async def _call(sql: str) -> str:
        return sql

    return tag_mcp_tool(StructuredTool(name=name, description=f"{name} tool", args_schema=_Args, coroutine=_call))


def test_off_by_default_keeps_the_configured_exclude():
    cfg = ToolSearchConfig(enabled=True, exclude=["github_find_issues"])
    assert deferral_exclude(cfg, {"bq_query"}) == ["github_find_issues"]


def test_switch_adds_the_schedule_allowlist():
    cfg = ToolSearchConfig(enabled=True, exclude=["github_find_issues"], bind_schedule_allowlist=True)
    assert deferral_exclude(cfg, {"bq_query", "db_query", "github_find_issues"}) == ["github_find_issues", "bq_query", "db_query"]


def test_no_schedule_no_change():
    cfg = ToolSearchConfig(enabled=True, exclude=["x"], bind_schedule_allowlist=True)
    assert deferral_exclude(cfg, None) == ["x"]


def test_allowlisted_mcp_tool_is_bound_and_others_stay_deferred():
    cfg = ToolSearchConfig(enabled=True, bind_schedule_allowlist=True)
    tools = [_mcp("bq_query"), _mcp("gmail_read_thread")]
    final, setup = assemble_deferred_tools(tools, enabled=True, exclude=deferral_exclude(cfg, {"bq_query"}))
    assert "bq_query" not in setup.deferred_names
    assert "gmail_read_thread" in setup.deferred_names
    assert {t.name for t in final} >= {"bq_query", "gmail_read_thread"}


def test_whole_allowlist_bound_means_no_deferred_set():
    cfg = ToolSearchConfig(enabled=True, bind_schedule_allowlist=True)
    tools = [_mcp("bq_query"), _mcp("db_query")]
    _, setup = assemble_deferred_tools(tools, enabled=True, exclude=deferral_exclude(cfg, {"bq_query", "db_query"}))
    assert not setup.deferred_names

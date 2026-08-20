from langchain_core.tools import tool as as_tool
from langgraph.types import Command

from deerflow.tools.builtins.tool_search import DeferredToolCatalog, build_deferred_tool_setup, build_tool_search_tool
from deerflow.tools.mcp_metadata import is_mcp_tool, tag_mcp_tool


@as_tool
def mcp_calc(expression: str) -> str:
    "Evaluate arithmetic."
    return expression


@as_tool
def local_echo(text: str) -> str:
    "Echo text."
    return text


def test_is_mcp_tool_reads_metadata():
    assert is_mcp_tool(tag_mcp_tool(mcp_calc)) is True
    assert is_mcp_tool(local_echo) is False


def test_setup_disabled_returns_empty():
    setup = build_deferred_tool_setup([tag_mcp_tool(mcp_calc), local_echo], enabled=False)
    assert setup.tool_search_tool is None
    assert setup.deferred_names == frozenset()
    assert setup.catalog_hash is None


def test_setup_no_mcp_returns_empty():
    setup = build_deferred_tool_setup([local_echo], enabled=True)
    assert setup.tool_search_tool is None
    assert setup.deferred_names == frozenset()


def test_setup_builds_from_mcp_survivors():
    setup = build_deferred_tool_setup([tag_mcp_tool(mcp_calc), local_echo], enabled=True)
    assert setup.deferred_names == frozenset({"mcp_calc"})
    assert setup.tool_search_tool is not None
    assert setup.tool_search_tool.name == "tool_search"
    assert setup.catalog_hash


def test_tool_search_returns_command_with_hash_scoped_promotion():
    catalog = DeferredToolCatalog((mcp_calc,))
    ts = build_tool_search_tool(catalog)
    out = ts.invoke({"type": "tool_call", "name": "tool_search", "args": {"query": "select:mcp_calc"}, "id": "tc1"})
    assert isinstance(out, Command)
    promoted = out.update["promoted"]
    assert promoted == {"catalog_hash": catalog.hash, "names": ["mcp_calc"]}
    msg = out.update["messages"][0]
    assert msg.tool_call_id == "tc1" and msg.name == "tool_search"
    assert "mcp_calc" in msg.content


def test_tool_search_promotes_every_selected_tool():
    """``select:`` promotes all named tools -- the tool closure must not re-cap.

    ``DeferredToolCatalog.search`` already caps the ranked modes internally, so
    a second ``[:MAX_RESULTS]`` in the closure only truncates ``select:``. Its
    sibling closure, ``skills/describe.py::describe_skill``, calls
    ``catalog.search(name)`` with no slice. Without this test, dropping the cap
    inside ``search`` alone would still leave ``select:`` capped here.
    """

    def _t(name: str):
        @as_tool(name)
        def _f(query: str) -> str:
            "A deferred tool."
            return query

        return _f

    names = [f"mcp_t{i}" for i in range(6)]  # 6 > MAX_RESULTS
    catalog = DeferredToolCatalog(tuple(_t(n) for n in names))
    ts = build_tool_search_tool(catalog)

    out = ts.invoke({"type": "tool_call", "name": "tool_search", "args": {"query": "select:" + ",".join(names)}, "id": "tc3"})

    assert out.update["promoted"]["names"] == names


def test_tool_search_no_match_empty_names():
    catalog = DeferredToolCatalog((mcp_calc,))
    ts = build_tool_search_tool(catalog)
    out = ts.invoke({"type": "tool_call", "name": "tool_search", "args": {"query": "select:nonexistent"}, "id": "tc2"})
    assert out.update["promoted"]["names"] == []
    assert "No tools found matching" in out.update["messages"][0].content


def test_tool_search_directly_bound_tool_does_not_claim_runtime_visibility():
    """Build-time binding does not imply that runtime skill policy kept the schema."""
    catalog = DeferredToolCatalog((mcp_calc,))
    ts = build_tool_search_tool(catalog, directly_bound_names=frozenset({"calendar_list_events", "pythia_query"}))
    out = ts.invoke({"type": "tool_call", "name": "tool_search", "args": {"query": "select:calendar_list_events"}, "id": "tc4"})
    assert out.update["promoted"]["names"] == []
    msg = out.update["messages"][0]
    assert "configured as directly bound, not deferred" in msg.content
    assert "only if its schema is present" in msg.content
    assert "current runtime policy does not allow it" in msg.content
    assert "Do NOT retry tool_search" in msg.content
    assert "already directly available and active" not in msg.content
    assert "calendar_list_events" in msg.content


def _named_mcp_tool(name: str):
    """A tagged MCP tool with an explicit name (mirrors the mcp_calc fixture)."""

    def _fn(text: str) -> str:
        """Echo."""
        return text

    t = as_tool(name)(_fn)
    tag_mcp_tool(t)
    return t


class TestExclude:
    """tool_search.exclude: hot MCP tools stay always-bound (patch #46)."""

    def test_excluded_tool_stays_bound(self):
        from deerflow.tools.builtins.tool_search import assemble_deferred_tools

        hot = _named_mcp_tool("pythia_query")
        cold = _named_mcp_tool("atlas_get_status")
        final, setup = assemble_deferred_tools([hot, cold], enabled=True, exclude=["pythia_*"])
        assert "pythia_query" not in setup.deferred_names
        assert "atlas_get_status" in setup.deferred_names
        assert any(t.name == "pythia_query" for t in final)

    def test_all_excluded_is_empty_setup_not_error(self):
        from deerflow.tools.builtins.tool_search import assemble_deferred_tools

        hot = _named_mcp_tool("kb_query")
        final, setup = assemble_deferred_tools([hot], enabled=True, exclude=["kb_query"])
        assert setup.tool_search_tool is None
        assert setup.deferred_names == frozenset()
        assert [t.name for t in final] == ["kb_query"]

    def test_exact_and_glob_patterns(self):
        from deerflow.tools.builtins.tool_search import _is_excluded

        assert _is_excluded("pythia_query", ["pythia_*"])
        assert _is_excluded("kb_query", ["kb_query"])
        assert not _is_excluded("atlas_get_status", ["pythia_*", "kb_query"])
        assert not _is_excluded("pythia_query", [])
        assert not _is_excluded("pythia_query", None)

    def test_config_default_empty(self):
        from deerflow.config.tool_search_config import ToolSearchConfig

        assert ToolSearchConfig().exclude == []
        cfg = ToolSearchConfig.model_validate({"enabled": True, "exclude": ["pythia_*"]})
        assert cfg.exclude == ["pythia_*"]

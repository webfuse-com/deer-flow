import json

from langchain.tools import ToolRuntime
from langchain_core.tools import tool as as_tool
from langgraph.types import Command

from deerflow.runtime.secret_context import SKILL_TOOL_POLICY_DECISION_CONTEXT_KEY
from deerflow.tools.builtins.custom_discovery import normalize_custom_descriptor, normalize_custom_provider_result
from deerflow.tools.builtins.tool_search import DeferredToolCatalog, build_tool_search_tool, resolve_custom_provider


@as_tool
def local_lookup(query: str) -> str:
    """Look up a local record."""
    return query


def _app_descriptor(name="app_function:atlas-test/demo/calculate"):
    return {
        "kind": "app_function",
        "name": name,
        "invocation_tool": "app_function_call",
        "invocation_args": {"app": "demo", "owner_stack": "atlas-test", "function": "calculate"},
        "input_schema": {"type": "object", "properties": {"value": {"type": "number"}}},
        "description": "Calculate a value",
        "effect": "read",
        "version": "sha256:abc",
    }


def test_custom_provider_resolution_rejects_invalid_configuration_type():
    assert resolve_custom_provider(None) is None
    assert resolve_custom_provider("") is None
    try:
        resolve_custom_provider(7)
    except TypeError as exc:
        assert "custom_provider" in str(exc)
    else:
        raise AssertionError("invalid custom_provider type must fail closed")


def test_custom_descriptor_validation_keeps_contract_and_drops_bad_records():
    descriptor = normalize_custom_descriptor({**_app_descriptor(), "output_schema": {"type": "string"}, "effect": "destructive"})
    assert descriptor["name"].startswith("app_function:")
    assert descriptor["output_schema"] == {"type": "string"}
    assert descriptor["effect"] == "destructive"
    assert normalize_custom_descriptor({**_app_descriptor(), "invocation_tool": "bash"}) is None
    assert normalize_custom_descriptor({**_app_descriptor(), "invocation_args": {"name": "x", "function": "f"}}) is None


def test_malformed_provider_records_are_partial_and_statuses_are_allowlisted():
    _, status = normalize_custom_provider_result({"results": [{"bad": True}], "providers": {"agora": "ok", "chronos": "timeout"}, "complete": True})
    assert status == {"providers": {"agora": "ok", "chronos": "unavailable"}, "complete": False}


def test_generic_tool_search_returns_local_and_custom_entries_without_promotion():
    calls = []

    def provider(query):
        calls.append(query)
        return {"results": [_app_descriptor()], "providers": {"agora": "ok"}, "complete": True}

    catalog = DeferredToolCatalog((local_lookup,))
    search = build_tool_search_tool(catalog, custom_provider=provider)
    result = search.invoke({"type": "tool_call", "name": "tool_search", "args": {"query": "local calculate"}, "id": "c1"})

    assert isinstance(result, Command)
    assert calls == ["local calculate"]
    payload = json.loads(result.update["messages"][0].content)
    assert payload[0]["name"] == "local_lookup"
    assert payload[1]["kind"] == "app_function"
    assert result.update["promoted"]["names"] == ["local_lookup"]
    assert "tool_search_timing" in result.update["messages"][0].additional_kwargs


def test_exact_mcp_selection_does_not_call_custom_provider():
    called = False

    def provider(query):
        nonlocal called
        called = True
        return [_app_descriptor()]

    search = build_tool_search_tool(DeferredToolCatalog((local_lookup,)), custom_provider=provider)
    result = search.invoke({"type": "tool_call", "name": "tool_search", "args": {"query": "select:local_lookup"}, "id": "c2"})
    assert not called
    assert result.update["promoted"]["names"] == ["local_lookup"]


def test_denied_custom_invokers_skip_provider_call():
    called = False

    def provider(query):
        nonlocal called
        called = True
        return [_app_descriptor()]

    search = build_tool_search_tool(DeferredToolCatalog((local_lookup,)), custom_provider=provider)
    runtime = ToolRuntime(
        state={},
        context={SKILL_TOOL_POLICY_DECISION_CONTEXT_KEY: {"allowed_names": ["tool_search"]}},
        config={},
        stream_writer=lambda _: None,
        tool_call_id="c-denied",
        store=None,
    )
    search.func("find an app", "c-denied", runtime)
    assert called is False


def test_namespaced_custom_selection_calls_provider_but_never_promotes_custom_name():
    descriptor = _app_descriptor()

    def provider(query):
        return {"results": [descriptor], "providers": {"agora": "ok"}, "complete": True}

    search = build_tool_search_tool(DeferredToolCatalog((local_lookup,)), custom_provider=provider)
    result = search.invoke({"type": "tool_call", "name": "tool_search", "args": {"query": "select:" + descriptor["name"]}, "id": "c3"})
    assert result.update["promoted"]["names"] == []
    assert json.loads(result.update["messages"][0].content)[0]["kind"] == "app_function"


def test_exact_selection_reports_only_runtime_visible_names_as_already_visible():
    search = build_tool_search_tool(
        DeferredToolCatalog((local_lookup,)),
        directly_bound_names=frozenset({"configured_but_hidden"}),
        runtime_visible_names=frozenset({"runtime_visible"}),
    )
    visible = search.invoke({"type": "tool_call", "name": "tool_search", "args": {"query": "select:runtime_visible"}, "id": "c-visible"})
    assert "already visible in the current tool list" in visible.update["messages"][0].content
    hidden = search.invoke({"type": "tool_call", "name": "tool_search", "args": {"query": "select:configured_but_hidden"}, "id": "c-hidden"})
    assert "not visible under the current runtime policy" in hidden.update["messages"][0].content


def test_provider_failure_is_partial_and_keeps_local_results():
    def provider(query):
        raise RuntimeError("catalog down")

    search = build_tool_search_tool(DeferredToolCatalog((local_lookup,)), custom_provider=provider)
    result = search.invoke({"type": "tool_call", "name": "tool_search", "args": {"query": "local"}, "id": "c4"})
    message = result.update["messages"][0]
    payload, _ = json.JSONDecoder().raw_decode(message.content)
    assert payload[0]["name"] == "local_lookup"
    assert payload[-1] == {
        "kind": "discovery_status",
        "status": "partial",
        "local_results_complete": True,
        "providers": {"custom": "unavailable"},
    }
    assert message.additional_kwargs["tool_search_partial"] is True
    assert message.additional_kwargs["tool_search_timing"]["provider_status"]["complete"] is False


def test_exact_selection_content_reports_unknown_and_visible_names_as_status_entry():
    search = build_tool_search_tool(
        DeferredToolCatalog((local_lookup,)),
        runtime_visible_names=frozenset({"runtime_visible"}),
    )
    result = search.invoke(
        {
            "type": "tool_call",
            "name": "tool_search",
            "args": {"query": "select:local_lookup,runtime_visible,missing_tool"},
            "id": "c5",
        }
    )
    payload = json.loads(result.update["messages"][0].content)
    assert payload[0]["name"] == "local_lookup"
    assert payload[-1] == {
        "kind": "discovery_status",
        "status": "selection",
        "already_visible": ["runtime_visible"],
        "configured_not_visible": [],
        "unknown": ["missing_tool"],
    }


def test_generic_results_are_capped_across_local_and_custom_matches():
    local_tools = []
    for index in range(5):

        @as_tool(f"local_{index}")
        def local(value: str) -> str:
            """Search local records."""
            return value

        local_tools.append(local)

    custom = [{**_app_descriptor(), "name": f"app_function:stack/demo/run_{index}"} for index in range(5)]
    search = build_tool_search_tool(DeferredToolCatalog(tuple(local_tools)), custom_provider=lambda _: custom)
    result = search.invoke({"type": "tool_call", "name": "tool_search", "args": {"query": "records"}, "id": "c6"})
    assert len(json.loads(result.update["messages"][0].content)) == 5

"""Declared outcomes survive adapters and runtime without classifying document JSON."""

import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from mcp.types import CallToolResult, TextContent

from deerflow.agents.middlewares.tool_error_handling_middleware import ToolErrorHandlingMiddleware
from deerflow.agents.middlewares.tool_result_meta import TOOL_META_KEY
from deerflow.tools.operation_result import CONTRACT_KEY, CONTRACT_VERSION, preserve_operation_errors


def request(metadata=None, messages=(), args=None):
    return SimpleNamespace(tool=SimpleNamespace(metadata=metadata or {CONTRACT_KEY: CONTRACT_VERSION}), tool_call={"id": "new", "name": "fix_record", "args": args or {"old": "A"}}, state={"messages": list(messages)})


def receipt(status="failed", execution_state="not_started", next_action="revise_arguments"):
    return {"status": status, "execution_state": execution_state, "next_action": next_action, "reason_code": "invalid_arguments", "field_errors": [{"path": "replacements[0].old", "code": "required"}]}


@pytest.mark.parametrize("shape", ["json", "blocks", "artifact"])
def test_declared_failure_is_error_with_repair_guidance(shape):
    payload = receipt()
    message = ToolMessage(content=json.dumps(payload), tool_call_id="new")
    if shape == "blocks":
        message.content = [{"type": "text", "text": message.content}]
    if shape == "artifact":
        message.artifact = {"structured_content": payload}
        message.content = "Receipt"
    result = ToolErrorHandlingMiddleware().wrap_tool_call(request(), lambda _: message)
    assert result.status == "error"
    assert result.additional_kwargs[TOOL_META_KEY]["recommended_next_action"] == "rewrite_query"
    assert result.additional_kwargs["deerflow_operation"]["next_action"] == "revise_arguments"


@pytest.mark.parametrize(
    ("status", "state", "action", "expected"),
    [
        ("pending", "running", "check_status", "partial_success"),
        ("committed_index_pending", "committed", "check_status", "partial_success"),
        ("applied", "committed", "report", "success"),
        ("preview_ready", "not_committed", "report", "success"),
        ("failed", "unknown", "check_status", "error"),
    ],
)
def test_receipt_truth(status, state, action, expected):
    result = ToolErrorHandlingMiddleware().wrap_tool_call(request(), lambda _: ToolMessage(content=json.dumps(receipt(status, state, action)), tool_call_id="new"))
    assert result.additional_kwargs[TOOL_META_KEY]["status"] == expected
    assert result.additional_kwargs["deerflow_operation"]["execution_state"] == state


def test_ordinary_document_json_is_not_an_operation():
    result = ToolErrorHandlingMiddleware().wrap_tool_call(request(metadata={"ordinary": True}), lambda _: ToolMessage(content=json.dumps(receipt()), tool_call_id="new"))
    assert result.status == "success"
    assert "deerflow_operation" not in result.additional_kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", [True, False])
async def test_interceptor_preserves_mcp_is_error_before_adapter_throws(declared):
    declarations = {("server", "fix_record")} if declared else set()
    intercept = preserve_operation_errors(declarations)
    payload = receipt()
    wire = CallToolResult(isError=True, structuredContent=payload, content=[TextContent(type="text", text=json.dumps(payload))])

    async def handler(_):
        return wire

    req = SimpleNamespace(server_name="server", name="fix_record", runtime=SimpleNamespace(tool_call_id="call-1"))
    result = await intercept(req, handler)
    if declared:
        assert isinstance(result, ToolMessage)
        assert result.status == "error"
        assert result.artifact["structured_content"] == payload
        assert result.tool_call_id == "call-1"
    else:
        assert result is wire


def test_two_invalid_calls_bound_identical_retries_but_allow_changed_arguments():
    middleware = ToolErrorHandlingMiddleware()
    history = [HumanMessage(content="Correct the record")]
    for i in range(2):
        history.append(AIMessage(content="", tool_calls=[{"id": str(i), "name": "fix_record", "args": {"old": "A"}}]))
        history.append(middleware.wrap_tool_call(request(), lambda _: ToolMessage(content=json.dumps(receipt()), tool_call_id=str(i))))
    invoked = []

    def handler(_):
        invoked.append(True)
        return ToolMessage(content=json.dumps(receipt("applied", "committed", "report")), tool_call_id="new")

    blocked = middleware.wrap_tool_call(request(messages=history), handler)
    assert invoked == []
    assert blocked.status == "error"
    assert blocked.additional_kwargs[TOOL_META_KEY]["recoverable_by_model"] is True
    changed = middleware.wrap_tool_call(request(messages=history, args={"old": "B"}), handler)
    assert changed.status == "success"
    assert invoked == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "sse"])
async def test_real_adapter_conversion_retains_structured_error(monkeypatch, transport):
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    from langchain_mcp_adapters.tools import convert_mcp_tool_to_langchain_tool
    from langgraph.graph import END, START, MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode
    from mcp.types import Tool

    payload = receipt()
    session = SimpleNamespace(initialize=AsyncMock(), call_tool=AsyncMock(return_value=CallToolResult(isError=True, structuredContent=payload, content=[TextContent(type="text", text=json.dumps(payload))])))

    @asynccontextmanager
    async def create_session(*a, **kw):
        yield session

    monkeypatch.setattr("langchain_mcp_adapters.tools.create_session", create_session)
    tool = convert_mcp_tool_to_langchain_tool(
        None,
        Tool(name="fix_record", description="Fix record", inputSchema={"type": "object", "properties": {}}, _meta={CONTRACT_KEY: CONTRACT_VERSION}),
        connection={"transport": transport, "url": "http://fixture/mcp"},
        server_name="server",
        tool_interceptors=[preserve_operation_errors({("server", "fix_record")})],
    )
    graph = StateGraph(MessagesState)
    graph.add_node("tools", ToolNode([tool]))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    result = await graph.compile().ainvoke({"messages": [AIMessage(content="", tool_calls=[{"name": "fix_record", "args": {}, "id": "fixture-call"}])]})
    message = result["messages"][-1]
    assert message.status == "error"
    assert message.artifact["structured_content"] == payload
    assert message.tool_call_id == "fixture-call"
    assert tool.metadata["_meta"][CONTRACT_KEY] == CONTRACT_VERSION


@pytest.mark.parametrize(
    "metadata,payload",
    [
        ({CONTRACT_KEY: "operation/2"}, receipt("applied", "committed", "report")),
        ({CONTRACT_KEY: CONTRACT_VERSION}, {**receipt("applied", "committed", "report"), "result_contract": "operation/2"}),
    ],
)
def test_unknown_contract_cannot_claim_success(metadata, payload):
    result = ToolErrorHandlingMiddleware().wrap_tool_call(request(metadata=metadata), lambda _: ToolMessage(content=json.dumps(payload), tool_call_id="new"))
    assert result.status == "error"
    assert result.additional_kwargs[TOOL_META_KEY]["error_type"] == "invalid_result_contract"
    assert json.loads(result.content)["status"] == "failed"
    assert result.additional_kwargs["deerflow_tool_transforms"][-1]["kind"] == "invalid_operation_receipt"


def test_uncertain_execution_state_cannot_claim_applied():
    result = ToolErrorHandlingMiddleware().wrap_tool_call(request(), lambda _: ToolMessage(content=json.dumps(receipt("applied", "unknown", "check_status")), tool_call_id="new"))
    assert result.status == "error"
    assert json.loads(result.content)["execution_state"] == "unknown"

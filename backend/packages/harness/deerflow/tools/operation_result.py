"""Opt-in operation receipts, independent of tool names or business domains.

Only tools declaring deerflow_result_contract=operation/1 in their catalog
metadata use this interpretation. Arbitrary fetched JSON is never a receipt.
"""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

CONTRACT_KEY = "deerflow_result_contract"
CONTRACT_VERSION = "operation/1"
OPERATION_KEY = "deerflow_operation"
META_KEY = "deerflow_tool_meta"
_STATES = {"not_started", "not_committed", "running", "committed", "unknown", "read_only"}
_ACTIONS = {"revise_arguments", "read_record", "check_status", "report", "continue"}
_FAILED = {"failed", "rejected", "needs_input"}
_PENDING = {"pending", "applying", "committed_index_pending"}
_DONE = {"applied", "no_change", "preview_ready"}


def contract_version(metadata):
    if not isinstance(metadata, dict):
        return None
    nested = metadata.get("_meta")
    return metadata.get(CONTRACT_KEY) or (nested.get(CONTRACT_KEY) if isinstance(nested, dict) else None)


def declared(metadata) -> bool:
    return contract_version(metadata) == CONTRACT_VERSION


def _receipt(message: ToolMessage) -> dict | None:
    artifact = message.artifact
    if isinstance(artifact, dict) and isinstance(artifact.get("structured_content"), dict):
        return artifact["structured_content"]
    content = message.content
    if isinstance(content, list):
        texts = [block["text"] for block in content if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)]
        content = "".join(texts)
    if not isinstance(content, str):
        return None
    try:
        parsed = json.loads(content)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def stamp_operation(message: ToolMessage, metadata) -> ToolMessage:
    version = contract_version(metadata)
    if version is None:
        return message
    value = _receipt(message) or {}
    status, state, action = value.get("status"), value.get("execution_state"), value.get("next_action")
    valid = (
        version == CONTRACT_VERSION
        and value.get("result_contract", CONTRACT_VERSION) == CONTRACT_VERSION
        and isinstance(state, str)
        and state in _STATES
        and isinstance(action, str)
        and action in _ACTIONS
        and (status is None and state == "read_only" or isinstance(status, str) and status in _FAILED | _PENDING | _DONE)
    )
    valid = valid and (status not in {"applied", "no_change"} or state == "committed") and (status != "preview_ready" or state == "not_committed")
    if not valid:
        runtime_status, state, action = "error", "unknown", "report"
        message.content = json.dumps(
            {
                "status": "failed",
                "execution_state": state,
                "next_action": action,
                "reason": "The tool returned an unsupported or invalid operation receipt. Report this before another write.",
                **{key: value[key] for key in ("operation_id", "record_ref") if isinstance(value.get(key), str)},
            }
        )
    elif status in _FAILED or message.status == "error":
        runtime_status = "error"
    elif status in _PENDING:
        runtime_status = "partial_success"
    else:
        runtime_status = "success"
    repairable = action in {"revise_arguments", "read_record", "check_status"}
    meta = {
        "status": runtime_status,
        "source": "tool_return",
        "error_type": "operation" if valid else "invalid_result_contract",
        "recoverable_by_model": valid,
        "recommended_next_action": "rewrite_query" if repairable else "continue" if runtime_status == "success" else "summarize",
    }
    operation = {"execution_state": state, "next_action": action}
    if valid:
        operation["status"] = status
    message.additional_kwargs = {**message.additional_kwargs, META_KEY: meta, OPERATION_KEY: operation}
    message.status = "error" if runtime_status == "error" else "success"
    return message


def repeated_invalid_call(request) -> ToolMessage | None:
    """Stop a third unchanged invalid call in the current user turn.

    State is already isolated by thread; no process-wide argument cache, raw
    argument logging, or automatic write retries are introduced.
    """
    if not declared(getattr(getattr(request, "tool", None), "metadata", None)):
        return None
    state = getattr(request, "state", None)
    if not isinstance(state, dict):
        return None
    calls = {}
    invalid = 0
    current = request.tool_call
    for message in state.get("messages", []):
        if isinstance(message, HumanMessage):
            calls.clear()
            invalid = 0
        elif isinstance(message, AIMessage):
            for call in message.tool_calls:
                calls[call.get("id")] = call
        elif isinstance(message, ToolMessage):
            call = calls.get(message.tool_call_id, {})
            operation = message.additional_kwargs.get(OPERATION_KEY, {})
            if operation.get("next_action") == "revise_arguments" and operation.get("execution_state") == "not_started" and call.get("name") == current.get("name") and call.get("args") == current.get("args"):
                invalid += 1
    if invalid < 2:
        return None
    message = ToolMessage(
        content=json.dumps(
            {
                "status": "failed",
                "reason_code": "identical_invalid_attempts",
                "reason": "These unchanged arguments have already failed twice. Read the record or report the missing information before preparing a different correction.",
                "execution_state": "not_started",
                "next_action": "report",
            }
        ),
        tool_call_id=str(current.get("id") or "missing_tool_call_id"),
        name=current.get("name"),
        status="error",
    )
    return stamp_operation(message, {CONTRACT_KEY: CONTRACT_VERSION})


def preserve_operation_errors(declarations: set[tuple[str, str]]):
    """Keep structured MCP errors intact before either adapter converts them.

    The discovery-owned set binds the raw server/tool identity. Returning a
    ToolMessage bypasses langchain-mcp-adapters' lossy isError -> ToolException
    path for HTTP/SSE as well as pooled stdio. Durable task drivers are separate.
    """

    async def intercept(request, handler):
        result = await handler(request)
        payload = getattr(result, "structuredContent", None)
        if (request.server_name, request.name) not in declarations or not getattr(result, "isError", False) or not isinstance(payload, dict):
            return result
        tool_call_id = getattr(request.runtime, "tool_call_id", None)
        # Direct non-graph callers use the same lossless content conversion.
        return ToolMessage(content=json.dumps(payload), tool_call_id=tool_call_id or "direct_mcp_call", name=request.name, status="error", artifact={"structured_content": payload})

    return intercept

"""[argus patch #143] ToolProgressMiddleware cause-aware blocking.

``tool_progress.cause_aware_blocking`` (default false) re-grades results by
cause before the state machine: argument/schema rejections are model-fixable,
bash is graded by its exit code, only an auth failure on a tool that has not
worked in this run blocks at once, and dispatcher tools keep their state per
function. atlas-quinten b4471658 (2026-10-16) lost 9 of 10 lead writes because
one malformed ``atlas_app_function_call`` blocked the tool for the run.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from langchain_core.messages import ToolMessage

from deerflow.agents.middlewares.tool_progress_middleware import (
    COMMAND_FAILED_ERROR_TYPE,
    INVALID_ARGS_ERROR_TYPE,
    ToolProgressMiddleware,
    regrade_tool_meta,
)
from deerflow.agents.middlewares.tool_result_meta import TOOL_META_KEY, ToolResultMeta, normalize_tool_message, tool_reported_status
from deerflow.config.tool_progress_config import ToolProgressConfig

_DISPATCHERS = ToolProgressConfig().dispatcher_tools
_MCP_MISSING = "McpError: missing required argument(s) for app_function_call: function"


def _runtime(thread_id: str = "t1", run_id: str = "r1", journal=None) -> MagicMock:
    rt = MagicMock()
    rt.context = {"thread_id": thread_id, "run_id": run_id}
    if journal is not None:
        rt.context["__run_journal"] = journal
    return rt


def _request(tool_name: str, runtime, args: dict | None = None, call_id: str = "tc") -> SimpleNamespace:
    return SimpleNamespace(tool_call={"name": tool_name, "id": call_id, "args": args or {}}, runtime=runtime)


def _result(content: str, *, tool_name: str, status: str = "success", call_id: str = "tc") -> ToolMessage:
    """A tool result stamped by the real shared classifier."""
    return normalize_tool_message(ToolMessage(content=content, tool_call_id=call_id, name=tool_name, status=status))


def _middleware(**kwargs) -> ToolProgressMiddleware:
    return ToolProgressMiddleware(cause_aware_blocking=True, dispatcher_tools=_DISPATCHERS, **kwargs)


def _call(mw: ToolProgressMiddleware, tool_name: str, runtime, content: str, *, args: dict | None = None, status: str = "success"):
    calls: list[str] = []

    def handler(_request):
        calls.append(tool_name)
        return _result(content, tool_name=tool_name, status=status)

    out = mw.wrap_tool_call(_request(tool_name, runtime, args), handler)
    return out, bool(calls)


def _meta(**overrides) -> ToolResultMeta:
    base = {"status": "error", "error_type": "config", "recoverable_by_model": False, "recommended_next_action": "stop", "source": "tool_return"}
    base.update(overrides)
    return ToolResultMeta(**base)


# ---------------------------------------------------------------------------
# Defaults keep upstream


def test_switch_is_off_by_default_and_upstream_still_blocks_at_once():
    cfg = ToolProgressConfig()
    assert cfg.cause_aware_blocking is False
    mw = ToolProgressMiddleware.from_config(cfg)
    rt = _runtime()
    _call(mw, "atlas_app_function_call", rt, _MCP_MISSING, args={"app": "leads"}, status="error")
    out, ran = _call(mw, "atlas_app_function_call", rt, '{"ok": true}', args={"app": "leads", "function": "update_lead"})
    assert not ran
    assert out.content.startswith("[TOOL_BLOCKED] Tool is not configured")


def test_from_config_passes_the_switch_and_dispatchers():
    mw = ToolProgressMiddleware.from_config(ToolProgressConfig(cause_aware_blocking=True, dispatcher_tools={"x_call": ["fn"]}))
    assert mw._cause_aware is True
    assert mw._dispatcher_tools == {"x_call": ("fn",)}


# ---------------------------------------------------------------------------
# Re-grading


def test_argument_errors_regrade_as_invalid_args():
    for text in (
        _MCP_MISSING,
        "HTTP 500: dispatch failed: Additional properties are not allowed ('query' was unexpected)",
        "'function' is a required property",
        "1 validation error for get_leads\nsegment\n  Field required [type=missing]",
        "invalid inputs: segment must be one of ...",
    ):
        meta = _parse(_result(text, tool_name="atlas_app_function_call", status="error"))
        regraded = regrade_tool_meta(meta, "atlas_app_function_call", text)
        assert regraded.error_type == INVALID_ARGS_ERROR_TYPE, text
        assert regraded.recoverable_by_model is True
        assert regraded.recommended_next_action == "rewrite_query"


def test_auth_and_rate_limit_grades_are_not_regraded():
    text = "401 Unauthorized: missing required header"
    meta = _meta(error_type="auth")
    assert regrade_tool_meta(meta, "github_get_issue", text) is meta
    meta = _meta(error_type="rate_limited", recommended_next_action="summarize")
    assert regrade_tool_meta(meta, "github_get_issue", "rate limited: invalid inputs") is meta


def test_bash_is_graded_by_exit_code_not_output_text():
    traceback = "Traceback (most recent call last):\n  ...\npytesseract.TesseractNotFoundError: tesseract is not installed\nExit Code: 1"
    meta = _parse(_result(traceback, tool_name="bash"))
    assert meta.error_type == "config"  # the shared classifier's keyword grade
    regraded = regrade_tool_meta(meta, "bash", traceback)
    assert regraded.error_type == COMMAND_FAILED_ERROR_TYPE
    assert regraded.recoverable_by_model is True

    clean = "id,name\nno results found for segment x"
    meta = _parse(_result(clean, tool_name="bash"))
    assert meta.status == "partial_success"
    assert regrade_tool_meta(meta, "bash", clean).status == "success"


def test_bash_tool_level_errors_keep_their_grade():
    text = "Error: Unexpected error initializing sandbox: sockproxy: GET /info is not on the allowlist"
    meta = _parse(_result(text, tool_name="bash"))
    assert regrade_tool_meta(meta, "bash", text) is meta
    exc = _meta(error_type="internal", source="exception")
    assert regrade_tool_meta(exc, "bash", "boom") is exc
    # The sandbox audit returns a blocked command with status="error" and no exit code.
    blocked = "Command blocked: command too long (9,000 characters; the limit is 8,000). Nothing ran."
    message = _result(blocked, tool_name="bash", status="error")
    meta = _parse(message)
    assert meta.status == "error"
    assert regrade_tool_meta(meta, "bash", blocked, tool_status=tool_reported_status(message)) is meta


# ---------------------------------------------------------------------------
# Blocking policy


def test_one_malformed_call_does_not_block_a_working_tool():
    """atlas-quinten b4471658: 11 good calls, 6 parallel calls without `function`, then correct calls."""
    mw = _middleware()
    rt = _runtime()
    for _ in range(11):
        _call(mw, "atlas_app_function_call", rt, '{"items": [1, 2]}', args={"app": "leads", "function": "get_leads"})
    for _ in range(6):
        out, ran = _call(mw, "atlas_app_function_call", rt, _MCP_MISSING, args={"app": "leads"}, status="error")
        assert ran
    for _ in range(10):
        out, ran = _call(mw, "atlas_app_function_call", rt, '{"ok": true}', args={"app": "leads", "function": "update_lead"})
        assert ran, "a correct call must reach the tool"
        assert "[TOOL_BLOCKED]" not in out.content


def test_repeated_invalid_args_warn_with_a_schema_hint_and_never_block():
    mw = _middleware()
    rt = _runtime()
    for _ in range(8):
        out, ran = _call(mw, "github_create_issue", rt, "'title' is a required property", status="error")
        assert ran
    hints = mw._drain_pending(rt)
    assert any("rejecting the call's arguments" in hint for hint in hints)


def test_bash_failure_in_output_text_does_not_block_bash():
    mw = _middleware()
    rt = _runtime()
    _call(mw, "bash", rt, "ok")
    out, ran = _call(mw, "bash", rt, "ImportError: tesseract is not installed\nExit Code: 1")
    assert ran
    out, ran = _call(mw, "bash", rt, "ok")
    assert ran and out.content == "ok"


def test_config_and_internal_errors_escalate_instead_of_blocking_at_once():
    mw = _middleware()
    rt = _runtime()
    text = "Error: Unexpected error initializing sandbox: sockproxy: GET /info is not on the allowlist"
    for attempt in range(5):
        out, ran = _call(mw, "ls", rt, text)
        assert ran, f"attempt {attempt + 1} must still run"
    out, ran = _call(mw, "ls", rt, "a\nb")
    assert not ran
    assert out.content.startswith("[TOOL_BLOCKED] Repeated internal errors")
    assert "Last error: Error: Unexpected error initializing sandbox" in out.content


def test_auth_blocks_at_once_only_when_the_tool_never_worked_in_this_run():
    mw = _middleware()
    rt = _runtime()
    _call(mw, "gmail_search", rt, "Error: 401 Unauthorized")
    out, ran = _call(mw, "gmail_search", rt, "[]")
    assert not ran and "Authentication failure" in out.content

    mw = _middleware()
    rt = _runtime()
    _call(mw, "gmail_search", rt, '[{"id": 1}]')
    out, ran = _call(mw, "gmail_search", rt, "Error: 401 Unauthorized")
    assert ran
    out, ran = _call(mw, "gmail_search", rt, '[{"id": 2}]')
    assert ran, "a tool that worked earlier in the run is not blocked on one auth failure"


def test_dispatcher_blocks_only_the_failing_function():
    journal = MagicMock()
    mw = _middleware()
    rt = _runtime(journal=journal)
    for _ in range(5):
        _call(mw, "app_function_call", rt, "Error: internal error in search_cards", args={"app": "kairos", "function": "search_cards"})
    out, ran = _call(mw, "app_function_call", rt, "{}", args={"app": "kairos", "function": "search_cards"})
    assert not ran
    assert "Only calls for kairos/search_cards are blocked" in out.content
    out, ran = _call(mw, "app_function_call", rt, '{"ok": true}', args={"app": "kairos", "function": "upsert_card"})
    assert ran and out.content == '{"ok": true}'

    blocks = [c.kwargs for c in journal.record_middleware.call_args_list if c.kwargs["action"] == "block"]
    assert blocks and blocks[-1]["changes"]["tool_name"] == "app_function_call"
    assert blocks[-1]["changes"]["dispatch_key"] == "app_function_call[kairos/search_cards]"


def test_dispatcher_args_as_json_string_and_missing_function():
    mw = _middleware()
    assert mw._state_key("connector_call", '{"name": "slack", "function": "post"}') == "connector_call[slack/post]"
    assert mw._state_key("app_function_call", {"app": "leads"}) == "app_function_call[leads/(missing)]"
    assert mw._state_key("bash", {"command": "ls"}) == "bash"
    assert ToolProgressMiddleware(dispatcher_tools=_DISPATCHERS)._state_key("app_function_call", {"app": "a", "function": "f"}) == "app_function_call"


def test_new_run_forgets_earlier_successes():
    mw = _middleware()
    rt = _runtime()
    _call(mw, "gmail_search", rt, "[]")
    mw._reset_run_states(rt, hook="before_agent")
    _call(mw, "gmail_search", rt, "Error: 401 Unauthorized")
    out, ran = _call(mw, "gmail_search", rt, "[]")
    assert not ran


def _parse(message: ToolMessage) -> ToolResultMeta:
    return ToolResultMeta(**message.additional_kwargs[TOOL_META_KEY])

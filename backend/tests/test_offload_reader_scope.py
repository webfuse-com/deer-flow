"""[argus patch #144] An allowlisted run can read its own offloaded tool results.

``tool_output.read_offloads_in_allowlisted_runs`` (default false) binds
``read_file`` for a run whose tool allowlist left it out, and
``OffloadReaderScopeMiddleware`` refuses every path outside the thread's
offload directory. atlas-quinten's outreach and ICP schedules and
atlas-yauhen's Kairos sync got offloaded results they could not read.

The scope is the current run: only a path a result of this run was offloaded
to is readable (a schedule on a Telegram-bound or ``thread:`` chat must not
read the offloads of earlier interactive runs, made with tools the schedule
was not granted), and the read happens on the host, where the budget
middleware wrote the file, so an app-only schedule never starts a sandbox.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from deerflow.agents.middlewares import tool_output_budget_middleware as budget_module
from deerflow.agents.middlewares.offload_reader_middleware import (
    READ_FILE_TOOL_NAME,
    OffloadReaderScopeMiddleware,
    offload_directory,
    offload_reader_grant,
)
from deerflow.agents.middlewares.tool_transform_meta import append_tool_transform
from deerflow.config.tool_output_config import ToolOutputConfig
from deerflow.sandbox.tools import READ_FILE_START_LINE_EXCEEDS, read_file_tool
from deerflow.skills.tool_policy import filter_tools_by_agent_allowed_tools

_OFFLOAD = "/mnt/user-data/outputs/.tool-results/app_function_call-3f2a-0c1d.txt"
_EARLIER = "/mnt/user-data/outputs/.tool-results/gmail_read_thread-77aa-19be.txt"


def _tool(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name)


def _app_config(**output) -> SimpleNamespace:
    return SimpleNamespace(tool_output=ToolOutputConfig(**output))


def _offloaded(path: str, *, tool: str = "app_function_call", call_id: str = "tc-a") -> ToolMessage:
    """A tool result as the budget middleware leaves it: a preview naming the saved file."""
    kwargs: dict = {}
    append_tool_transform(kwargs, "externalized", by="ToolOutputBudgetMiddleware")
    return ToolMessage(content=f"[{tool} returned 167,312 chars; the full result is saved to {path}. Use read_file on {path}.]\nfirst rows ...", tool_call_id=call_id, name=tool, additional_kwargs=kwargs)


def _this_run(*paths: str) -> list:
    return [HumanMessage("Refresh the lead list"), AIMessage(""), *[_offloaded(path, call_id=f"tc-{i}") for i, path in enumerate(paths)]]


def _request(path: object, name: str = READ_FILE_TOOL_NAME, *, messages: list | None = None, outputs_path: str | None = None, args: dict | None = None) -> SimpleNamespace:
    call_args = dict(args or {})
    if path is not None:
        call_args["path"] = path
    state: dict = {"messages": _this_run(_OFFLOAD) if messages is None else messages}
    if outputs_path is not None:
        state["thread_data"] = {"outputs_path": outputs_path}
    return SimpleNamespace(tool_call={"name": name, "id": "tc-1", "args": call_args}, runtime=SimpleNamespace(state=state, context={}), state=state)


def _handler(calls: list):
    def handler(request):
        calls.append(request.tool_call["args"].get("path"))
        return ToolMessage(content="file body", tool_call_id="tc-1", name=request.tool_call["name"])

    return handler


# ---------------------------------------------------------------------------
# Grant


def test_off_by_default():
    assert ToolOutputConfig().read_offloads_in_allowlisted_runs is False
    candidates = [_tool("atlas_app_function_call"), _tool("read_file")]
    allowed = [candidates[0]]
    tools, scope = offload_reader_grant(candidates, allowed, _app_config())
    assert tools is allowed and scope is None


def test_allowlist_without_read_file_gets_the_real_tool_and_a_scope():
    candidates = [read_file_tool, _tool("atlas_app_function_call"), _tool("bash")]
    allowed = filter_tools_by_agent_allowed_tools(candidates, None, extra_allowed={"atlas_app_function_call"})
    assert [t.name for t in allowed] == ["atlas_app_function_call"]
    tools, scope = offload_reader_grant(candidates, allowed, _app_config(read_offloads_in_allowlisted_runs=True))
    assert [t.name for t in tools] == ["atlas_app_function_call", "read_file"]
    assert tools[-1] is read_file_tool
    assert isinstance(scope, OffloadReaderScopeMiddleware)


def test_no_grant_when_read_file_is_allowed_absent_or_offload_disabled():
    on = _app_config(read_offloads_in_allowlisted_runs=True)
    with_reader = [_tool("bq_query"), _tool("read_file")]
    assert offload_reader_grant(with_reader, with_reader, on) == (with_reader, None)

    no_reader = [_tool("bq_query")]
    assert offload_reader_grant(no_reader, no_reader, on) == (no_reader, None)

    disabled = _app_config(read_offloads_in_allowlisted_runs=True, enabled=False)
    allowed = [_tool("bq_query")]
    assert offload_reader_grant([*allowed, _tool("read_file")], allowed, disabled) == (allowed, None)


def test_scope_follows_the_configured_storage_subdir():
    _, scope = offload_reader_grant([_tool("read_file")], [], _app_config(read_offloads_in_allowlisted_runs=True, storage_subdir="cache"))
    assert scope.allows("/mnt/user-data/outputs/cache/bash-1.txt")
    assert not scope.allows("/mnt/user-data/outputs/.tool-results/bash-1.txt")
    assert offload_directory(".tool-results") == "/mnt/user-data/outputs/.tool-results"


# ---------------------------------------------------------------------------
# Scope


def test_without_a_host_copy_the_offload_is_read_through_the_real_handler():
    # A remote sandbox keeps the offload inside the sandbox; no outputs path here.
    scope = OffloadReaderScopeMiddleware(offload_directory(".tool-results"))
    calls: list = []
    out = scope.wrap_tool_call(_request(_OFFLOAD), _handler(calls))
    assert calls == [_OFFLOAD]
    assert out.content == "file body"


def test_an_offload_from_an_earlier_run_in_the_same_chat_is_refused():
    """A Telegram-bound schedule must not read what an interactive Gmail run offloaded."""
    scope = OffloadReaderScopeMiddleware(offload_directory(".tool-results"))
    messages = [
        HumanMessage("Summarize the Harel thread"),
        _offloaded(_EARLIER, tool="gmail_read_thread", call_id="tc-old"),
        AIMessage("Here is the summary."),
        *_this_run(_OFFLOAD),
    ]
    calls: list = []
    refused = scope.wrap_tool_call(_request(_EARLIER, messages=messages), _handler(calls))
    assert calls == []
    assert refused.status == "error"
    assert "offloaded during this run" in refused.content
    allowed = scope.wrap_tool_call(_request(_OFFLOAD, messages=messages), _handler(calls))
    assert calls == [_OFFLOAD] and allowed.content == "file body"


def test_a_path_only_mentioned_by_a_result_is_not_an_offload_of_this_run():
    scope = OffloadReaderScopeMiddleware(offload_directory(".tool-results"))
    mention = ToolMessage(content=f"See {_EARLIER} for the earlier export.", tool_call_id="tc-m", name="app_function_call")
    calls: list = []
    out = scope.wrap_tool_call(_request(_EARLIER, messages=[HumanMessage("go"), mention]), _handler(calls))
    assert calls == [] and out.status == "error"


def test_hidden_control_messages_do_not_end_the_run():
    scope = OffloadReaderScopeMiddleware(offload_directory(".tool-results"))
    hidden = HumanMessage("continue", additional_kwargs={"hide_from_ui": True})
    messages = [*_this_run(_OFFLOAD), hidden]
    calls: list = []
    scope.wrap_tool_call(_request(_OFFLOAD, messages=messages), _handler(calls))
    assert calls == [_OFFLOAD]


def test_the_blob_reference_names_the_offload_too():
    scope = OffloadReaderScopeMiddleware(offload_directory(".tool-results"))
    kwargs: dict = {budget_module.TOOL_OUTPUT_BLOB_KEY: {"virtual_path": _OFFLOAD, "storage_subdir": ".tool-results"}}
    append_tool_transform(kwargs, "externalized", by="ToolOutputBudgetMiddleware")
    result = ToolMessage(content="[preview without the path]", tool_call_id="tc-b", name="app_function_call", additional_kwargs=kwargs)
    calls: list = []
    scope.wrap_tool_call(_request(_OFFLOAD, messages=[HumanMessage("go"), result]), _handler(calls))
    assert calls == [_OFFLOAD]


# ---------------------------------------------------------------------------
# Host-side read (no sandbox)


def _real_offload(monkeypatch, tmp_path, content: str) -> tuple[str, ToolMessage, str]:
    """Externalize a result with the real budget middleware, on the host."""
    monkeypatch.setattr(budget_module, "get_blob_store_if_enabled", lambda: None)
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    message = ToolMessage(content=content, tool_call_id="tc-real", name="app_function_call")
    patched = budget_module._patch_tool_message(message, ToolOutputConfig(externalize_min_chars=100), str(outputs))
    path = next(word.strip(".,;:()[]`'\"") for word in str(patched.content).split() if word.startswith("/mnt/user-data/outputs/.tool-results/"))
    return str(outputs), patched, path


@pytest.fixture
def no_sandbox(monkeypatch):
    import deerflow.sandbox.tools as sandbox_tools

    def refuse(*_args, **_kwargs):
        raise AssertionError("an offload read must not initialize a sandbox")

    monkeypatch.setattr(sandbox_tools, "ensure_sandbox_initialized", refuse)
    monkeypatch.setattr(sandbox_tools, "ensure_sandbox_initialized_async", refuse)


def test_the_offload_is_read_on_the_host_without_a_sandbox(monkeypatch, tmp_path, no_sandbox):
    rows = "\n".join(f"lead {i}: Gemeente {i}, stage open" for i in range(40))
    outputs, offloaded, path = _real_offload(monkeypatch, tmp_path, rows)
    scope = OffloadReaderScopeMiddleware(offload_directory(".tool-results"))

    def never(_request):
        raise AssertionError("the sandbox read_file must not run")

    messages = [HumanMessage("Refresh the lead list"), offloaded]
    whole = scope.wrap_tool_call(_request(path, messages=messages, outputs_path=outputs), never)
    assert whole.content == rows and whole.name == READ_FILE_TOOL_NAME and whole.status != "error"
    ranged = scope.wrap_tool_call(_request(path, messages=messages, outputs_path=outputs, args={"start_line": 3, "end_line": 4}), never)
    assert ranged.content == "lead 2: Gemeente 2, stage open\nlead 3: Gemeente 3, stage open"
    past_end = scope.wrap_tool_call(_request(path, messages=messages, outputs_path=outputs, args={"start_line": 500}), never)
    assert past_end.content == READ_FILE_START_LINE_EXCEEDS

    async def never_async(_request):
        raise AssertionError("the sandbox read_file must not run")

    awaited = asyncio.run(scope.awrap_tool_call(_request(path, messages=messages, outputs_path=outputs), never_async))
    assert awaited.content == rows


def test_a_missing_host_copy_falls_back_to_the_tool(monkeypatch, tmp_path):
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    scope = OffloadReaderScopeMiddleware(offload_directory(".tool-results"))
    calls: list = []
    out = scope.wrap_tool_call(_request(_OFFLOAD, outputs_path=str(outputs)), _handler(calls))
    assert calls == [_OFFLOAD] and out.content == "file body"


def test_paths_outside_the_offload_directory_are_refused_before_the_tool_runs():
    scope = OffloadReaderScopeMiddleware(offload_directory(".tool-results"))
    for path in (
        "/mnt/user-data/outputs/report.md",
        "/mnt/user-data/outputs/.tool-results/../report.md",
        "/mnt/user-data/outputs/.tool-results/",
        "/mnt/user-data/outputs/.tool-results/sub/x.txt",
        "/mnt/skills/public/knowledge-edit/SKILL.md",
        ".tool-results/app_function_call-3f2a.txt",
        "",
        None,
        123,
    ):
        calls: list = []
        out = scope.wrap_tool_call(_request(path), _handler(calls))
        assert calls == [], path
        assert out.status == "error"
        assert out.content.startswith("Error: read_file in this run can only read offloaded tool results in /mnt/user-data/outputs/.tool-results/")


def test_other_tools_pass_untouched():
    scope = OffloadReaderScopeMiddleware(offload_directory(".tool-results"))
    calls: list = []
    scope.wrap_tool_call(_request("/etc/passwd", name="bash"), _handler(calls))
    assert calls == ["/etc/passwd"]


def test_async_path_applies_the_same_scope():
    scope = OffloadReaderScopeMiddleware(offload_directory(".tool-results"))
    calls: list = []

    async def handler(request):
        calls.append(request.tool_call["args"]["path"])
        return ToolMessage(content="ok", tool_call_id="tc-1", name=READ_FILE_TOOL_NAME)

    refused = asyncio.run(scope.awrap_tool_call(_request("/mnt/user-data/workspace/x.py"), handler))
    allowed = asyncio.run(scope.awrap_tool_call(_request(_OFFLOAD), handler))
    assert refused.status == "error" and allowed.content == "ok"
    assert calls == [_OFFLOAD]


# ---------------------------------------------------------------------------
# Lead-agent assembly


def _assemble(monkeypatch, *, switch: bool):
    from unittest.mock import MagicMock, create_autospec

    import deerflow.tools as tools_module
    from deerflow.agents.lead_agent import agent as lead_agent_module
    from deerflow.config.app_config import AppConfig
    from deerflow.config.model_config import ModelConfig
    from deerflow.config.sandbox_config import SandboxConfig
    from deerflow.config.tool_policy_config import ToolPolicyConfig

    app_config = AppConfig(
        models=[ModelConfig(name="safe-model", display_name="safe-model", description=None, use="langchain_openai:ChatOpenAI", model="safe-model", supports_thinking=False, supports_vision=False)],
        sandbox=SandboxConfig(use="deerflow.sandbox.local:LocalSandboxProvider"),
        tool_policy=ToolPolicyConfig(source="agent"),
        tool_output=ToolOutputConfig(read_offloads_in_allowlisted_runs=switch),
    )

    def _named(name: str):
        tool = MagicMock()
        tool.name = name
        return tool

    monkeypatch.setattr(lead_agent_module, "get_app_config", lambda: app_config)
    monkeypatch.setattr(tools_module, "get_available_tools", lambda **kwargs: [_named("read_file"), _named("atlas_app_function_call"), _named("bash")])
    build = create_autospec(lead_agent_module.build_middlewares, return_value=[])
    monkeypatch.setattr(lead_agent_module, "build_middlewares", build)
    monkeypatch.setattr(lead_agent_module, "apply_prompt_template", lambda **kwargs: "prompt")
    monkeypatch.setattr(lead_agent_module, "create_chat_model", lambda **kwargs: object())
    monkeypatch.setattr(lead_agent_module, "create_agent", lambda **kwargs: kwargs)
    result = lead_agent_module.make_lead_agent(
        {
            "context": {
                "model_name": "safe-model",
                "thinking_enabled": False,
                "subagent_enabled": False,
                "non_interactive": True,
                "allowed_tools": ["atlas_app_function_call"],
            }
        }
    )
    return [tool.name for tool in result["tools"]], build.call_args.kwargs.get("custom_middlewares")


def test_scheduled_run_without_read_file_gets_the_scoped_reader(monkeypatch):
    names, custom = _assemble(monkeypatch, switch=True)
    assert names == ["atlas_app_function_call", "read_file"]
    assert len(custom) == 1 and isinstance(custom[0], OffloadReaderScopeMiddleware)


def test_scheduled_run_keeps_its_allowlist_with_the_switch_off(monkeypatch):
    names, custom = _assemble(monkeypatch, switch=False)
    assert names == ["atlas_app_function_call"]
    assert custom is None

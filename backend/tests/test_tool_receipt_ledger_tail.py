"""[argus patch #155] The receipt ledger follows the cache-stable durable-context layout.

The ledger is a window over the latest tool receipts ("older receipts omitted"),
so it changes on every model call. Inserted right after the system prompt, it
made SGLang's prefix cache recompute the whole conversation on each call: after
a subagent returned in atlas-nicholas thread 4447dd29 (2026-10-23) every lead
call re-read 40-70k tokens, and every subagent call re-read up to 60k (first
token 0.7 s -> 6.4 s). With the #125/#147 tail layout on for the chain's model,
the ledger renders at the end of the request instead.
"""

from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from deerflow.agents.middlewares.tool_receipt import TOOL_RECEIPT_KEY
from deerflow.agents.middlewares.tool_receipt_middleware import ToolReceiptMiddleware
from deerflow.agents.middlewares.tool_result_meta import TOOL_META_KEY
from deerflow.config.app_config import AppConfig
from deerflow.config.model_config import ModelConfig
from deerflow.config.prompt_cache_config import PromptCacheConfig
from deerflow.config.sandbox_config import SandboxConfig


def _call(n: int) -> list:
    tool_call_id = f"c{n}"
    result = ToolMessage(content=f"out {n}", tool_call_id=tool_call_id, name="bash")
    result.additional_kwargs = {
        TOOL_META_KEY: {"status": "success"},
        TOOL_RECEIPT_KEY: {
            "tool_call_id": tool_call_id,
            "tool_name": "bash",
            "status": "success",
            "args_sha256": f"{n:016x}",
            "output_sha256": f"{n + 1:016x}",
            "output_bytes": 5,
            "created_at": "2026-10-23T14:20:00+00:00",
        },
    }
    return [AIMessage(content="", tool_calls=[{"name": "bash", "args": {"cmd": str(n)}, "id": tool_call_id, "type": "tool_call"}]), result]


def _history(calls: int) -> list:
    messages = [SystemMessage(content="sys"), HumanMessage(content="go")]
    for n in range(1, calls + 1):
        messages += _call(n)
    return messages


def _rendered(middleware: ToolReceiptMiddleware, messages: list) -> list:
    captured = {}
    request = SimpleNamespace(messages=messages, override=lambda **kwargs: SimpleNamespace(**kwargs))

    def handler(req):
        captured["messages"] = req.messages
        return SimpleNamespace(result=[AIMessage(content="ok")])

    middleware.wrap_model_call(request, handler)
    return captured["messages"]


def _ledger_index(messages: list) -> int:
    return next(i for i, m in enumerate(messages) if isinstance(m, HumanMessage) and m.additional_kwargs.get("deerflow_tool_receipt_context"))


def test_default_keeps_the_ledger_after_the_system_prompt():
    messages = _rendered(ToolReceiptMiddleware(), _history(2))
    assert _ledger_index(messages) == 1


def test_tail_puts_the_ledger_after_the_newest_tool_result():
    messages = _rendered(ToolReceiptMiddleware(at_tail=True), _history(2))
    assert _ledger_index(messages) == len(messages) - 1
    assert isinstance(messages[-2], ToolMessage) and messages[-2].tool_call_id == "c2"


def test_tail_keeps_a_trailing_user_request_last():
    messages = _rendered(ToolReceiptMiddleware(at_tail=True), [*_history(1), AIMessage(content="done"), HumanMessage(content="next")])
    assert messages[-1].content == "next"
    assert _ledger_index(messages) == len(messages) - 2


def test_tail_keeps_the_prompt_before_the_newest_messages_identical():
    """The window slides by one receipt per call (30 receipts overflow the budget);
    only the tail may differ between two consecutive calls."""
    middleware = ToolReceiptMiddleware(at_tail=True)
    first = _rendered(middleware, _history(30))
    second = _rendered(middleware, _history(31))
    ledger = _ledger_index(first)
    assert "older receipts omitted" in first[ledger].content
    assert [m.content for m in second[:ledger]] == [m.content for m in first[:ledger]]
    assert second[_ledger_index(second)].content != first[ledger].content


def test_head_layout_changes_the_prefix_on_every_call():
    """The 2026-10-23 behaviour this patch removes for tail-layout models."""
    middleware = ToolReceiptMiddleware()
    first = _rendered(middleware, _history(30))
    second = _rendered(middleware, _history(31))
    assert first[1].content != second[1].content


def _model(name: str) -> ModelConfig:
    return ModelConfig(name=name, display_name=name, description=None, use="langchain_openai:ChatOpenAI", model=name, supports_thinking=False, supports_vision=False)


def _app_config(volatile_at_tail: bool) -> AppConfig:
    return AppConfig(
        models=[_model("local-qwen"), _model("claude-opus-5.5")],
        sandbox=SandboxConfig(use="test"),
        prompt_cache=PromptCacheConfig(durable_context_volatile_at_tail=volatile_at_tail),
    )


def _receipt_layer(middlewares: list) -> ToolReceiptMiddleware:
    return next(mw for mw in middlewares if isinstance(mw, ToolReceiptMiddleware))


def test_lead_and_subagent_chains_follow_the_durable_context_layout(monkeypatch):
    from deerflow.agents.lead_agent import agent as lead_agent_module
    from deerflow.agents.middlewares.tool_error_handling_middleware import build_lead_runtime_middlewares, build_subagent_runtime_middlewares

    on, off = _app_config(True), _app_config(False)
    assert _receipt_layer(build_lead_runtime_middlewares(app_config=on, receipts_at_tail=True, lazy_init=False))._at_tail is True
    assert _receipt_layer(build_lead_runtime_middlewares(app_config=on, lazy_init=False))._at_tail is False
    assert _receipt_layer(build_subagent_runtime_middlewares(app_config=on, model_name="local-qwen", lazy_init=False))._at_tail is True
    assert _receipt_layer(build_subagent_runtime_middlewares(app_config=on, model_name="claude-opus-5.5", lazy_init=False))._at_tail is False
    assert _receipt_layer(build_subagent_runtime_middlewares(app_config=off, model_name="local-qwen", lazy_init=False))._at_tail is False

    # The lead's build_middlewares asks for the tail only for a tail-layout model (#147).
    seen = []
    monkeypatch.setattr(lead_agent_module, "build_lead_runtime_middlewares", lambda **kwargs: seen.append(kwargs) or [])
    monkeypatch.setattr(lead_agent_module, "_create_summarization_middleware", lambda **kwargs: None)
    monkeypatch.setattr(lead_agent_module, "_create_todo_list_middleware", lambda is_plan_mode, **kwargs: None)
    run = {"configurable": {"is_plan_mode": False, "subagent_enabled": False}}
    for app_config, model_name in ((on, "local-qwen"), (on, "claude-opus-5.5"), (off, "local-qwen")):
        lead_agent_module.build_middlewares(run, model_name=model_name, app_config=app_config)
    assert [kwargs.get("receipts_at_tail", False) for kwargs in seen] == [True, False, False]

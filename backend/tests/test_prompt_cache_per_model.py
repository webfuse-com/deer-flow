"""[argus patch #147] Durable-context layout per model, and a stable head from call 1.

Patch #125 moved the changing parts of durable context to the tail of the
request for every model on a stack. That helps SGLang's radix cache
(local-qwen extends went 76% -> 83%) and breaks cloud prompt caches, which
reuse a prefix only where an earlier request ended: gpt-5.6-sol extends went
57% -> 7%, claude-opus-5.5 37% -> 11% (Ariadne, 2026-10-13/19). The tail
layout is now chosen per model (``prompt_cache.volatile_tail_models``,
default ``["local-qwen"]``), and the authority contract can be part of the
system message from the first call instead of appearing mid-run.
"""

from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from deerflow.agents.middlewares.durable_context_middleware import _DURABLE_CONTEXT_DATA_KEY, DurableContextMiddleware
from deerflow.config.app_config import AppConfig
from deerflow.config.model_config import ModelConfig
from deerflow.config.prompt_cache_config import PromptCacheConfig, durable_context_layout
from deerflow.config.sandbox_config import SandboxConfig


def _model(name: str, model: str | None = None) -> ModelConfig:
    return ModelConfig(name=name, display_name=name, description=None, use="langchain_openai:ChatOpenAI", model=model or name, supports_thinking=False, supports_vision=False)


def _app_config(**prompt_cache) -> AppConfig:
    return AppConfig(
        models=[_model("local-qwen"), _model("claude-opus-5.5"), _model("my-radix", model="local-qwen"), _model("gemini-3.8-flash")],
        sandbox=SandboxConfig(use="test"),
        prompt_cache=PromptCacheConfig(**prompt_cache),
    )


def _inject(middleware, messages, state):
    return middleware._inject(SimpleNamespace(messages=messages, state=state, override=lambda **kwargs: SimpleNamespace(**kwargs))).messages


def _data(messages):
    return [(i, m.content) for i, m in enumerate(messages) if isinstance(m, HumanMessage) and m.additional_kwargs.get(_DURABLE_CONTEXT_DATA_KEY)]


# -- config ---------------------------------------------------------------


def test_defaults_keep_upstream_layout_for_every_model():
    config = PromptCacheConfig()
    assert config.volatile_tail_models == ["local-qwen"]
    assert config.volatile_tail_for("local-qwen") is False
    assert config.contract_always() is False


def test_switch_on_puts_only_matched_models_on_the_tail():
    config = PromptCacheConfig(durable_context_volatile_at_tail=True)
    assert config.volatile_tail_for("local-qwen") is True
    assert config.volatile_tail_for("claude-opus-5.5") is False
    assert config.volatile_tail_for("gpt-5.6-sol") is False


def test_patterns_match_the_config_name_or_the_model_id():
    config = PromptCacheConfig(durable_context_volatile_at_tail=True, volatile_tail_models=["local-*"])
    assert config.volatile_tail_for("local-qwen-thinking") is True
    assert config.volatile_tail_for("my-radix", provider_model="local-qwen") is True
    assert config.volatile_tail_for("glm-nw", provider_model="glm-5.3") is False


def test_star_restores_the_stack_wide_switch_and_empty_turns_it_off():
    assert PromptCacheConfig(durable_context_volatile_at_tail=True, volatile_tail_models=["*"]).volatile_tail_for("claude-opus-5.5") is True
    assert PromptCacheConfig(durable_context_volatile_at_tail=True, volatile_tail_models=[]).volatile_tail_for("local-qwen") is False


def test_a_chain_without_a_model_keeps_the_patch_125_reading():
    assert PromptCacheConfig(durable_context_volatile_at_tail=True).volatile_tail_for(None) is True
    assert PromptCacheConfig().volatile_tail_for(None) is False


def test_contract_follows_the_switch_unless_set():
    assert PromptCacheConfig(durable_context_volatile_at_tail=True).contract_always() is True
    assert PromptCacheConfig(durable_context_volatile_at_tail=True, durable_context_contract_always=False).contract_always() is False
    assert PromptCacheConfig(durable_context_contract_always=True).contract_always() is True


def test_existing_boolean_configs_still_parse():
    """The five stacks that set the switch false (and the template's true) need no edit to load."""
    assert PromptCacheConfig.model_validate({"durable_context_volatile_at_tail": False}).volatile_tail_for("local-qwen") is False
    assert PromptCacheConfig.model_validate({"durable_context_volatile_at_tail": True}).volatile_tail_for("local-qwen") is True


def test_layout_resolves_the_model_id_from_the_app_config():
    app_config = _app_config(durable_context_volatile_at_tail=True)
    assert durable_context_layout(app_config, "local-qwen") == (True, True)
    assert durable_context_layout(app_config, "claude-opus-5.5") == (False, True)
    assert durable_context_layout(app_config, "my-radix") == (True, True)
    assert durable_context_layout(app_config, "unknown-model") == (False, True)


def test_layout_reads_a_stub_config_as_upstream():
    assert durable_context_layout(SimpleNamespace(), "local-qwen") == (False, False)
    assert durable_context_layout(SimpleNamespace(prompt_cache=SimpleNamespace(durable_context_volatile_at_tail=True)), "local-qwen") == (False, False)


# -- middleware -------------------------------------------------------------

EMPTY = {"summary_text": None, "goal": None}
ARTIFACT = {"tool_artifacts": [{"handle": "art_1", "display_name": "report.html", "artifact_type": "file", "status": "available"}]}


def test_contract_is_in_the_head_before_any_data_exists():
    messages = _inject(DurableContextMiddleware(contract_always=True), [SystemMessage(content="sys"), HumanMessage(content="go")], EMPTY)
    assert [type(m) for m in messages] == [SystemMessage, SystemMessage, HumanMessage]
    assert messages[1].content.startswith("## Durable context authority contract")
    assert _data(messages) == []


def test_without_contract_always_an_empty_state_is_left_alone():
    request = SimpleNamespace(messages=[SystemMessage(content="sys"), HumanMessage(content="go")], state=EMPTY, override=lambda **kwargs: SimpleNamespace(**kwargs))
    assert DurableContextMiddleware()._inject(request) is request


def test_the_system_head_does_not_change_when_the_first_data_appears():
    """Cause A of the cache report: the contract used to appear at the call that produced the first artifact."""
    middleware = DurableContextMiddleware(contract_always=True)
    step1 = [SystemMessage(content="sys"), HumanMessage(content="go")]
    first = _inject(middleware, step1, EMPTY)
    step2 = [*step1, AIMessage(content="", tool_calls=[{"name": "bash", "args": {}, "id": "c1", "type": "tool_call"}]), ToolMessage(content="ok", tool_call_id="c1")]
    second = _inject(middleware, step2, {**EMPTY, **ARTIFACT})
    assert [m.content for m in second[:2]] == [m.content for m in first[:2]]


def test_cloud_model_head_layout_is_an_extension_while_data_is_unchanged():
    """With the head layout, request N+1 starts with all of request N when the data block did not change."""
    middleware = DurableContextMiddleware(volatile_at_tail=False, contract_always=True)
    step1 = [SystemMessage(content="sys"), HumanMessage(content="go"), AIMessage(content="", tool_calls=[{"name": "bash", "args": {}, "id": "c1", "type": "tool_call"}]), ToolMessage(content="ok", tool_call_id="c1")]
    first = _inject(middleware, step1, {**EMPTY, **ARTIFACT})
    step2 = [*step1, AIMessage(content="", tool_calls=[{"name": "bash", "args": {}, "id": "c2", "type": "tool_call"}]), ToolMessage(content="ok2", tool_call_id="c2")]
    second = _inject(middleware, step2, {**EMPTY, **ARTIFACT})
    assert [m.content for m in second[: len(first)]] == [m.content for m in first]


def test_tail_layout_is_never_an_extension_of_the_previous_request():
    """Why the tail layout is local-only: the previous tail sits where the next request has new messages."""
    middleware = DurableContextMiddleware(volatile_at_tail=True, contract_always=True)
    step1 = [SystemMessage(content="sys"), HumanMessage(content="go"), AIMessage(content="", tool_calls=[{"name": "bash", "args": {}, "id": "c1", "type": "tool_call"}]), ToolMessage(content="ok", tool_call_id="c1")]
    first = _inject(middleware, step1, {**EMPTY, **ARTIFACT})
    step2 = [*step1, AIMessage(content="", tool_calls=[{"name": "bash", "args": {}, "id": "c2", "type": "tool_call"}]), ToolMessage(content="ok2", tool_call_id="c2")]
    second = _inject(middleware, step2, {**EMPTY, **ARTIFACT})
    assert [m.content for m in second[: len(first)]] != [m.content for m in first]
    assert [m.content for m in second[: len(first) - 1]] == [m.content for m in first[:-1]]


# -- wiring -----------------------------------------------------------------


def _durable(middlewares):
    (durable,) = [m for m in middlewares if isinstance(m, DurableContextMiddleware)]
    return durable


def test_subagent_chain_uses_its_own_model():
    from deerflow.agents.middlewares.tool_error_handling_middleware import build_subagent_runtime_middlewares

    app_config = _app_config(durable_context_volatile_at_tail=True)
    local = _durable(build_subagent_runtime_middlewares(app_config=app_config, model_name="local-qwen", lazy_init=False))
    cloud = _durable(build_subagent_runtime_middlewares(app_config=app_config, model_name="claude-opus-5.5", lazy_init=False))
    assert (local._volatile_at_tail, local._contract_always) == (True, True)
    assert (cloud._volatile_at_tail, cloud._contract_always) == (False, True)


def test_lead_chain_uses_the_resolved_model(monkeypatch):
    from deerflow.agents.lead_agent import agent as lead_agent_module

    app_config = _app_config(durable_context_volatile_at_tail=True)
    monkeypatch.setattr(lead_agent_module, "get_app_config", lambda: app_config)
    monkeypatch.setattr(lead_agent_module, "_create_summarization_middleware", lambda **kwargs: None)
    monkeypatch.setattr(lead_agent_module, "_create_todo_list_middleware", lambda is_plan_mode, **kwargs: None)

    def build(model_name):
        return _durable(
            lead_agent_module.build_middlewares(
                {"configurable": {"is_plan_mode": False, "subagent_enabled": False}},
                model_name=model_name,
                app_config=app_config,
            )
        )

    assert (build("local-qwen")._volatile_at_tail, build("local-qwen")._contract_always) == (True, True)
    assert (build("gemini-3.8-flash")._volatile_at_tail, build("gemini-3.8-flash")._contract_always) == (False, True)


def test_lead_chain_default_config_is_upstream(monkeypatch):
    from deerflow.agents.lead_agent import agent as lead_agent_module

    app_config = _app_config()
    monkeypatch.setattr(lead_agent_module, "get_app_config", lambda: app_config)
    monkeypatch.setattr(lead_agent_module, "_create_summarization_middleware", lambda **kwargs: None)
    monkeypatch.setattr(lead_agent_module, "_create_todo_list_middleware", lambda is_plan_mode, **kwargs: None)
    durable = _durable(lead_agent_module.build_middlewares({"configurable": {"is_plan_mode": False, "subagent_enabled": False}}, model_name="local-qwen", app_config=app_config))
    assert (durable._volatile_at_tail, durable._contract_always) == (False, False)

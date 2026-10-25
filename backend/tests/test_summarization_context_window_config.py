"""[argus] Per-model compaction as upstream config (replaces retired patch #47).

Patch #47 sized the trigger and picked the summarizer per lead model through
``summarization.per_model``. The 2026-10-25 sync expresses the same policy with
upstream config: ``model_name: null`` (each run model summarizes itself), a
``fraction`` trigger resolved against the run model's profile, and a
``context_window`` on every model entry, which the model factory turns into
the LangChain profile's ``max_input_tokens``. These tests build the real
middleware from the real factories for the fleet-shaped config, including the
local-qwen alias, which has no provider profile of its own.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

import deerflow.agents.middlewares.summarization_middleware as summ_mod
import deerflow.config.summarization_config as summ_config_mod
from deerflow.agents.lead_agent import agent as agent_module
from deerflow.agents.middlewares.summarization_middleware import create_summarization_middleware
from deerflow.config.app_config import AppConfig
from deerflow.config.model_config import ModelConfig
from deerflow.config.sandbox_config import SandboxConfig
from deerflow.config.summarization_config import SummarizationConfig

# 0.7 * 137,143 = 96,000.1, which LangChain floors to the old 96,000 absolute trigger.
LOCAL_QWEN_WINDOW = 137_143
CLOUD_WINDOW = 1_000_000


def _model(name: str, context_window: int | None, **extra) -> ModelConfig:
    return ModelConfig(
        name=name,
        use="langchain_openai:ChatOpenAI",
        model=name,
        api_key="k",
        base_url="http://argus-litellm:4000/v1",
        supports_thinking=True,
        context_window=context_window,
        **extra,
    )


def _local_qwen() -> ModelConfig:
    # Shaped like the atlas-template entry: thinking off in the base body, on
    # only through the when_thinking_enabled overlay.
    return _model(
        "local-qwen",
        LOCAL_QWEN_WINDOW,
        extra_body={"top_k": 20, "chat_template_kwargs": {"enable_thinking": False}},
        when_thinking_enabled={"extra_body": {"top_k": 20, "chat_template_kwargs": {"enable_thinking": True}}},
    )


def _fleet_summarization(**overrides) -> dict:
    block = {
        "enabled": True,
        "model_name": None,
        "trigger": [{"type": "fraction", "value": 0.7}],
        "keep": {"type": "tokens", "value": 28000},
        "trim_tokens_to_summarize": None,
    }
    block.update(overrides)
    return block


def _app_config(*models: ModelConfig, summarization: dict | None = None) -> AppConfig:
    return AppConfig(
        models=list(models) or [_local_qwen(), _model("glm-nw", CLOUD_WINDOW)],
        sandbox=SandboxConfig(use="deerflow.community.aio_sandbox.aio_sandbox_provider:AioSandboxProvider"),
        summarization=SummarizationConfig.model_validate(summarization or _fleet_summarization()),
    )


def _fires(middleware, tokens: int) -> bool:
    # No AIMessage, so only the counted total can satisfy a clause.
    return middleware._should_summarize([HumanMessage(content="x")], tokens)


class TestFractionTriggerFromContextWindow:
    def test_local_qwen_alias_triggers_at_96k(self):
        middleware = create_summarization_middleware(app_config=_app_config(), run_model_name="local-qwen")

        assert middleware is not None
        assert middleware.model.profile["max_input_tokens"] == LOCAL_QWEN_WINDOW
        assert middleware.trigger == [("fraction", 0.7)]
        assert not _fires(middleware, 95_999)
        assert _fires(middleware, 96_000)

    def test_cloud_model_triggers_at_70_percent_of_its_own_window(self):
        middleware = create_summarization_middleware(app_config=_app_config(), run_model_name="glm-nw")

        assert middleware is not None
        assert not _fires(middleware, 96_000)
        assert not _fires(middleware, 699_999)
        assert _fires(middleware, 700_000)

    def test_each_lead_summarizes_itself(self):
        for run_model in ("local-qwen", "glm-nw"):
            middleware = create_summarization_middleware(app_config=_app_config(), run_model_name=run_model)

            assert middleware is not None
            assert middleware._generation_candidate_names() == [run_model]
            # Generation reuses the profile anchor instead of building a second model.
            assert middleware._model_for(run_model) is middleware._summary_model

    def test_lead_agent_factory_passes_the_run_model(self):
        middleware = agent_module._create_summarization_middleware(app_config=_app_config(), run_model_name="glm-nw")

        assert middleware is not None
        assert middleware.model.profile["max_input_tokens"] == CLOUD_WINDOW

    def test_model_without_context_window_loses_its_fraction_trigger(self, caplog):
        """Why every model entry needs ``context_window`` (and no backstop helps)."""
        config = _app_config(_local_qwen(), _model("no-window", None))

        with caplog.at_level(logging.WARNING, logger=summ_mod.__name__):
            middleware = create_summarization_middleware(app_config=config, run_model_name="no-window")

        assert middleware is not None
        assert middleware.trigger is None
        assert not _fires(middleware, 10_000_000)
        assert "auto-compaction will not fire" in caplog.text

    def test_trigger_clauses_are_ored_so_a_tokens_backstop_fires_for_every_model(self):
        """A ``tokens`` backstop is not scoped to profile-less models: LangChain ORs
        the clauses, so ``[fraction 0.7, tokens 96000]`` compacts a 1M-window model
        at 96k too. That is why the template carries no backstop."""
        config = _app_config(summarization=_fleet_summarization(trigger=[{"type": "fraction", "value": 0.7}, {"type": "tokens", "value": 96000}]))

        middleware = create_summarization_middleware(app_config=config, run_model_name="glm-nw")

        assert middleware is not None
        assert _fires(middleware, 96_000)


class TestSummarizerThinkingStaysOff:
    def test_summary_model_is_built_with_thinking_disabled(self, monkeypatch):
        calls: list[dict] = []
        real_create_chat_model = summ_mod.create_chat_model

        def recording_create_chat_model(**kwargs):
            calls.append(kwargs)
            return real_create_chat_model(**kwargs)

        monkeypatch.setattr(summ_mod, "create_chat_model", recording_create_chat_model)

        middleware = create_summarization_middleware(app_config=_app_config(), run_model_name="local-qwen")

        assert middleware is not None
        assert [(call["name"], call["thinking_enabled"]) for call in calls] == [("local-qwen", False)]
        # The base body's enable_thinking: false reaches the provider; the
        # when_thinking_enabled overlay is not applied.
        assert middleware.model.extra_body["chat_template_kwargs"] == {"enable_thinking": False}

    def test_summary_call_uses_the_thinking_off_model(self, monkeypatch):
        prompts: list[str] = []
        middleware = create_summarization_middleware(app_config=_app_config(), run_model_name="local-qwen", keep=("messages", 1), skip_memory_flush=True)
        assert middleware is not None

        class _Recorder:
            def invoke(self, prompt, config=None):
                prompts.append(prompt)
                return SimpleNamespace(text="## ACTIVE OBJECTIVE\nkeep going")

        monkeypatch.setattr(middleware, "_summary_model", _Recorder())
        messages = [HumanMessage(content="old request", id="u1"), AIMessage(content="old answer " * 50, id="a1"), HumanMessage(content="CURRENT", id="u2")]

        result = middleware.compact_state({"messages": messages}, SimpleNamespace(context={}), force=True)

        assert result is not None
        assert result.summary_text.startswith("## ACTIVE OBJECTIVE")
        assert len(prompts) == 1
        assert "<active_user_request>\nHuman: CURRENT\n</active_user_request>" in prompts[0]


class TestRetiredPerModelIsIgnored:
    _PER_MODEL = {"glm-nw": {"model_name": "glm-nw", "trigger": {"type": "tokens", "value": 733992}}}

    def test_fleet_block_with_per_model_loads_and_behaves_like_no_overrides(self, monkeypatch, caplog):
        monkeypatch.setattr(summ_config_mod, "_per_model_ignored_logged", False)
        legacy = _fleet_summarization(model_name="local-qwen", trigger={"type": "tokens", "value": 96000}, per_model=self._PER_MODEL)

        with caplog.at_level(logging.WARNING, logger=summ_config_mod.__name__):
            config = _app_config(summarization=legacy)
            SummarizationConfig.model_validate(legacy)

        assert not hasattr(config.summarization, "per_model")
        assert "per_model" not in config.summarization.model_dump()
        assert caplog.text.count("summarization.per_model is no longer supported") == 1
        middleware = create_summarization_middleware(app_config=config, run_model_name="glm-nw")
        assert middleware is not None
        # The glm-nw override no longer applies: the global trigger and summarizer do.
        assert middleware.trigger == ("tokens", 96000)
        assert middleware._generation_candidate_names() == ["local-qwen", "glm-nw"]

    def test_config_without_per_model_does_not_log(self, monkeypatch, caplog):
        monkeypatch.setattr(summ_config_mod, "_per_model_ignored_logged", False)

        with caplog.at_level(logging.WARNING, logger=summ_config_mod.__name__):
            SummarizationConfig.model_validate(_fleet_summarization())

        assert "per_model" not in caplog.text

    @pytest.mark.parametrize("value", [{}, None])
    def test_empty_or_null_per_model_is_accepted(self, monkeypatch, value):
        monkeypatch.setattr(summ_config_mod, "_per_model_ignored_logged", False)

        config = SummarizationConfig.model_validate({"enabled": True, "per_model": value})

        assert config.enabled is True

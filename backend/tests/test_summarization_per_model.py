"""[argus] patch #47: per-lead-model summarization overrides.

Why this exists: the summarization block is global, but the correct trigger and
the correct summarizer both depend on the LEAD model's context window. On argus,
`glm-planner` runs on `glm-nw` (1,048,560-token window per LiteLLM) while the
global config summarizes at 91,750 tokens on `local-qwen` (131,072) — so ~91% of
the paid-for context was discarded, and the summarizer could not have read the
thread it was compressing even if it had been allowed to.

These tests pin the resolution semantics (inherit-by-default, no mutation, no
recursion) and the wiring that passes the lead model into the middleware factory.
"""

import pytest

from deerflow.config.summarization_config import (
    ContextSize,
    SummarizationConfig,
    SummarizationOverride,
)


def _base() -> SummarizationConfig:
    """The argus-shaped global config: local-qwen summarizer, 91,750 trigger."""
    return SummarizationConfig(
        enabled=True,
        model_name="local-qwen",
        trigger=ContextSize(type="tokens", value=91750),
        keep=ContextSize(type="messages", value=20),
        trim_tokens_to_summarize=4000,
        per_model={
            "glm-nw": SummarizationOverride(
                model_name="glm-nw",
                trigger=ContextSize(type="tokens", value=734000),
            )
        },
    )


class TestResolvedFor:
    def test_no_lead_model_is_back_compatible(self):
        """None must behave exactly as before the patch: the global config."""
        cfg = _base()
        assert cfg.resolved_for(None) is cfg

    def test_model_without_override_is_unchanged(self):
        cfg = _base()
        assert cfg.resolved_for("local-qwen") is cfg

    def test_unknown_model_is_unchanged(self):
        cfg = _base()
        assert cfg.resolved_for("kimi-k3") is cfg

    def test_override_applies_summarizer_and_trigger(self):
        r = _base().resolved_for("glm-nw")
        assert r.model_name == "glm-nw"
        assert r.trigger.value == 734000

    def test_unset_override_fields_are_inherited(self):
        """An override that sets only the trigger must keep the global keep/trim."""
        cfg = SummarizationConfig(
            enabled=True,
            model_name="local-qwen",
            trigger=ContextSize(type="tokens", value=91750),
            keep=ContextSize(type="messages", value=20),
            trim_tokens_to_summarize=4000,
            per_model={"glm-nw": SummarizationOverride(trigger=ContextSize(type="tokens", value=734000))},
        )
        r = cfg.resolved_for("glm-nw")
        assert r.trigger.value == 734000
        assert r.model_name == "local-qwen"  # inherited
        assert r.keep.value == 20  # inherited
        assert r.trim_tokens_to_summarize == 4000  # inherited

    def test_empty_override_returns_self(self):
        cfg = SummarizationConfig(enabled=True, per_model={"glm-nw": SummarizationOverride()})
        assert cfg.resolved_for("glm-nw") is cfg

    def test_does_not_mutate_the_receiver(self):
        cfg = _base()
        cfg.resolved_for("glm-nw")
        assert cfg.model_name == "local-qwen"
        assert cfg.trigger.value == 91750

    def test_resolved_view_drops_per_model_so_it_cannot_recurse(self):
        r = _base().resolved_for("glm-nw")
        assert r.per_model == {}
        # resolving again is a no-op rather than a second application
        assert r.resolved_for("glm-nw") is r

    def test_enabled_and_other_globals_survive(self):
        r = _base().resolved_for("glm-nw")
        assert r.enabled is True
        assert r.skill_file_read_tool_names == ["read_file", "read", "view", "cat"]

    def test_keep_override_applies(self):
        cfg = SummarizationConfig(
            enabled=True,
            keep=ContextSize(type="messages", value=20),
            per_model={"glm-nw": SummarizationOverride(keep=ContextSize(type="messages", value=80))},
        )
        assert cfg.resolved_for("glm-nw").keep.value == 80

    def test_list_trigger_override_applies(self):
        cfg = SummarizationConfig(
            enabled=True,
            trigger=ContextSize(type="tokens", value=91750),
            per_model={
                "glm-nw": SummarizationOverride(
                    trigger=[
                        ContextSize(type="tokens", value=734000),
                        ContextSize(type="messages", value=400),
                    ]
                )
            },
        )
        r = cfg.resolved_for("glm-nw")
        assert isinstance(r.trigger, list)
        assert [t.value for t in r.trigger] == [734000, 400]

    def test_default_config_has_empty_per_model(self):
        """Back-compat: a config that never mentions per_model resolves to itself."""
        cfg = SummarizationConfig()
        assert cfg.per_model == {}
        assert cfg.resolved_for("glm-nw") is cfg


class TestMiddlewareWiring:
    """The factory must consult the override, and the caller must pass the model."""

    def test_factory_accepts_lead_model_name(self):
        import inspect

        from deerflow.agents.lead_agent import agent as agent_module

        sig = inspect.signature(agent_module._create_summarization_middleware)
        assert "lead_model_name" in sig.parameters, "factory must accept the lead model"
        assert sig.parameters["lead_model_name"].default is None, "must default to None (back-compat)"

    def test_build_middlewares_passes_the_resolved_model(self):
        """Guards the wiring: build_middlewares already resolves model_name for the
        vision middleware, and must hand the SAME value to summarization."""
        import inspect

        from deerflow.agents.lead_agent import agent as agent_module

        src = inspect.getsource(agent_module.build_middlewares)
        assert "_create_summarization_middleware(" in src
        assert "lead_model_name=model_name" in src, "must pass the resolved lead model, not a literal"

    def test_factory_resolves_the_override(self, monkeypatch):
        """End-to-end at the factory seam: a glm-nw lead must summarize on glm-nw."""
        from deerflow.agents.lead_agent import agent as agent_module

        created: dict[str, object] = {}

        # The real DeerFlowSummarizationMiddleware inspects the model
        # (`_llm_type`, token counting), so a bare stub is not enough. Record the
        # requested name and then stop before construction — the name is the
        # whole assertion.
        class _FakeModel:
            _llm_type = "chat"
            def with_config(self, *args, **kwargs):
                return self
            def get_num_tokens_from_messages(self, *args, **kwargs):
                return 10

        def fake_create_chat_model(*, name=None, **kwargs):
            created["name"] = name
            return _FakeModel()

        import deerflow.agents.middlewares.summarization_middleware as summ_mod
        monkeypatch.setattr(agent_module, "create_chat_model", fake_create_chat_model)
        monkeypatch.setattr(summ_mod, "create_chat_model", fake_create_chat_model)

        from deerflow.config.app_config import AppConfig
        from deerflow.config.model_config import ModelConfig
        from deerflow.config.sandbox_config import SandboxConfig
        _Cfg = AppConfig(
            models=[
                ModelConfig(name="local-qwen", use="langchain_openai:ChatOpenAI", model="local-qwen", api_key="k"),
                ModelConfig(name="glm-nw", use="langchain_openai:ChatOpenAI", model="glm-nw", api_key="k"),
            ],
            sandbox=SandboxConfig(use="deerflow.community.aio_sandbox.aio_sandbox_provider:AioSandboxProvider"),
            summarization=_base(),
        )

        mw = agent_module._create_summarization_middleware(app_config=_Cfg, lead_model_name="glm-nw")
        assert mw is not None
        assert created["name"] == "glm-nw", "the glm-nw override must select the glm-nw summarizer"

        created.clear()
        mw2 = agent_module._create_summarization_middleware(app_config=_Cfg, lead_model_name="local-qwen")
        assert mw2 is not None
        assert created["name"] == "local-qwen", "a model without an override keeps the global summarizer"

    def test_disabled_still_returns_none(self):
        from deerflow.agents.lead_agent import agent as agent_module

        class _Cfg:
            summarization = SummarizationConfig(enabled=False, per_model={})

        assert agent_module._create_summarization_middleware(app_config=_Cfg, lead_model_name="glm-nw") is None

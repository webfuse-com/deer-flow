"""Per-subagent thinking configuration.

Covers the config surface (custom agents and per-agent overrides), the
resolution helper, and the registry plumbing that carries the flag to
``SubagentExecutor._create_agent``. ``None`` must always preserve the
historical default (thinking off) so this change is inert until a subagent
opts in.
"""

from deerflow.config.subagents_config import (
    CustomSubagentConfig,
    SubagentOverrideConfig,
    SubagentsAppConfig,
    load_subagents_config_from_dict,
)
from deerflow.subagents.config import SubagentConfig, resolve_subagent_thinking


def _reset_subagents_config(**overrides) -> None:
    load_subagents_config_from_dict({"timeout_seconds": 900, **overrides})


class TestResolveSubagentThinking:
    def test_default_none_is_off(self):
        config = SubagentConfig(name="a", description="a", system_prompt="p")
        assert config.thinking_enabled is None
        assert resolve_subagent_thinking(config) is False

    def test_explicit_true(self):
        config = SubagentConfig(name="a", description="a", system_prompt="p", thinking_enabled=True)
        assert resolve_subagent_thinking(config) is True

    def test_explicit_false(self):
        config = SubagentConfig(name="a", description="a", system_prompt="p", thinking_enabled=False)
        assert resolve_subagent_thinking(config) is False


class TestConfigSurface:
    def test_custom_subagent_accepts_thinking(self):
        custom = CustomSubagentConfig(description="d", system_prompt="p", thinking_enabled=True)
        assert custom.thinking_enabled is True
        assert CustomSubagentConfig(description="d", system_prompt="p").thinking_enabled is None

    def test_override_defaults_none_and_accepts_bool(self):
        assert SubagentOverrideConfig().thinking_enabled is None
        assert SubagentOverrideConfig(thinking_enabled=True).thinking_enabled is True
        assert SubagentOverrideConfig(thinking_enabled=False).thinking_enabled is False

    def test_get_thinking_for(self):
        config = SubagentsAppConfig(agents={"bash": SubagentOverrideConfig(thinking_enabled=True)})
        assert config.get_thinking_for("bash") is True
        assert config.get_thinking_for("general-purpose") is None
        assert config.get_thinking_for("unknown") is None


class TestRegistryPlumbing:
    def teardown_method(self):
        _reset_subagents_config()

    def test_custom_agent_thinking_carried_into_registry(self):
        from deerflow.subagents.registry import get_subagent_config

        load_subagents_config_from_dict(
            {
                "timeout_seconds": 900,
                "custom_agents": {
                    "build": {
                        "description": "build worker",
                        "system_prompt": "p",
                        "model": "m1",
                        "thinking_enabled": True,
                    }
                },
            }
        )
        config = get_subagent_config("build")
        assert config is not None
        assert config.thinking_enabled is True
        assert resolve_subagent_thinking(config) is True

    def test_custom_agent_defaults_to_off(self):
        from deerflow.subagents.registry import get_subagent_config

        load_subagents_config_from_dict(
            {
                "timeout_seconds": 900,
                "custom_agents": {"build": {"description": "build worker", "system_prompt": "p"}},
            }
        )
        assert get_subagent_config("build").thinking_enabled is None

    def test_per_agent_override_applies_to_builtin(self):
        from deerflow.subagents.registry import get_subagent_config

        load_subagents_config_from_dict(
            {
                "timeout_seconds": 900,
                "agents": {"general-purpose": {"thinking_enabled": True}},
            }
        )
        assert get_subagent_config("general-purpose").thinking_enabled is True
        # Other agents keep their own value (None -> off).
        assert get_subagent_config("bash").thinking_enabled is None

    def test_per_agent_override_wins_over_custom_value(self):
        from deerflow.subagents.registry import get_subagent_config

        load_subagents_config_from_dict(
            {
                "timeout_seconds": 900,
                "custom_agents": {
                    "build": {
                        "description": "build worker",
                        "system_prompt": "p",
                        "thinking_enabled": False,
                    }
                },
                "agents": {"build": {"thinking_enabled": True}},
            }
        )
        assert get_subagent_config("build").thinking_enabled is True

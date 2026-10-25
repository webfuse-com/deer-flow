"""[argus patch #53] The agent's tool ceiling (``deerflow.tools.agent_tool_policy``).

Upstream #6935 made skill allowed-tools compatibility metadata, so the
agent's ``allowed_tools`` applies whenever declared and ``tool_policy.source``
is accepted and ignored. Locks the tri-state semantics of
AgentConfig.allowed_tools:

- None (undeclared) means NO restriction — a whitelist nobody declared
  cannot restrict. This is the opposite reading of "None = no tools", so it
  is pinned here explicitly.
- [] (declared empty) means no tools.
- [names] means exactly those, unioned with the firing schedule's
  allowed-tools (extra_allowed, argus patch #43).
- Skill allowed-tools declarations never grant or deny; a declaration
  outside a restrictive ceiling is logged, not granted.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

import pytest

from deerflow.config.agents_config import AgentConfig
from deerflow.config.tool_policy_config import ToolPolicyConfig
from deerflow.skills.types import Skill
from deerflow.tools.agent_tool_policy import filter_tools_by_agent_allowed_tools


@dataclass
class _Tool:
    name: str


def _make_skill(name: str, allowed_tools: list[str] | None = None) -> Skill:
    return Skill(
        name=name,
        description=f"Description for {name}",
        license="MIT",
        skill_dir=Path(f"/tmp/{name}"),
        skill_file=Path(f"/tmp/{name}/SKILL.md"),
        relative_path=Path(name),
        category="public",
        allowed_tools=allowed_tools,
        enabled=True,
    )


TOOLS = [_Tool("bash"), _Tool("read_file"), _Tool("atlas_update_schedule"), _Tool("chargebee_create_activation_invite")]


class TestAgentCeilingTriState:
    def test_none_means_no_restriction_even_with_declaring_skills(self):
        """The load-bearing case: skills declare, but an undeclared agent
        ceiling binds EVERYTHING (the old skill union would have stripped
        atlas_update_schedule here)."""
        skills = [_make_skill("restricted", ["read_file"])]
        result = filter_tools_by_agent_allowed_tools(TOOLS, None, skills=skills)
        assert result == TOOLS

    def test_declared_empty_means_no_tools(self):
        assert filter_tools_by_agent_allowed_tools(TOOLS, []) == []

    def test_declared_list_is_exact(self):
        result = filter_tools_by_agent_allowed_tools(TOOLS, ["bash", "read_file"])
        assert [t.name for t in result] == ["bash", "read_file"]


class TestScheduleUnion:
    def test_schedule_list_is_sole_whitelist_on_unrestricted_agent(self):
        """agent None + schedule list -> the schedule's list scopes the run.
        This is what keeps unattended scheduled runs scopable after the
        fleet moves to the agent source."""
        result = filter_tools_by_agent_allowed_tools(TOOLS, None, extra_allowed={"bash"})
        assert [t.name for t in result] == ["bash"]

    def test_schedule_unions_with_restrictive_agent(self):
        result = filter_tools_by_agent_allowed_tools(TOOLS, ["read_file"], extra_allowed={"bash"})
        assert [t.name for t in result] == ["bash", "read_file"]

    def test_schedule_unions_with_declared_empty_agent(self):
        result = filter_tools_by_agent_allowed_tools(TOOLS, [], extra_allowed={"bash"})
        assert [t.name for t in result] == ["bash"]


class TestSkillDeclarationsAreDocumentation:
    def test_skill_declaration_does_not_expand_a_restrictive_ceiling(self, caplog):
        skills = [_make_skill("wants-more", ["chargebee_create_activation_invite"])]
        with caplog.at_level(logging.WARNING):
            result = filter_tools_by_agent_allowed_tools(TOOLS, ["bash"], skills=skills)
        assert [t.name for t in result] == ["bash"]
        assert "wants-more" in caplog.text
        assert "chargebee_create_activation_invite" in caplog.text

    def test_no_warning_when_agent_is_unrestricted(self, caplog):
        skills = [_make_skill("wants-more", ["chargebee_create_activation_invite"])]
        with caplog.at_level(logging.WARNING):
            filter_tools_by_agent_allowed_tools(TOOLS, None, skills=skills)
        assert "wants-more" not in caplog.text

    def test_no_warning_when_declaration_fits_the_ceiling(self, caplog):
        skills = [_make_skill("fits", ["bash"])]
        with caplog.at_level(logging.WARNING):
            filter_tools_by_agent_allowed_tools(TOOLS, ["bash", "read_file"], skills=skills)
        assert "fits" not in caplog.text

    def test_persisted_restrictive_skill_cannot_remove_bash_from_unrestricted_agent(self):
        persisted = [_make_skill("ticket-management", ["read_file"])]

        result = filter_tools_by_agent_allowed_tools(TOOLS, None, skills=persisted)

        assert [tool.name for tool in result] == [tool.name for tool in TOOLS]


class TestConfigSurface:
    def test_tool_policy_source_defaults_to_unset(self):
        assert ToolPolicyConfig().source is None

    @pytest.mark.parametrize("source", ["agent", "skills", "everything"])
    def test_any_tool_policy_source_still_loads(self, source):
        """Fleet configs set ``source: agent``; old or odd values must keep loading."""
        assert ToolPolicyConfig(source=source).source == source

    def test_ignored_source_is_logged_once(self, caplog, monkeypatch):
        import deerflow.config.tool_policy_config as tool_policy_config

        monkeypatch.setattr(tool_policy_config, "_IGNORED_SOURCES_LOGGED", set())
        with caplog.at_level(logging.WARNING):
            ToolPolicyConfig(source="skills")
            ToolPolicyConfig(source="skills")
            ToolPolicyConfig(source="agent")
        assert caplog.text.count("tool_policy.source='skills' is ignored") == 1
        assert "'agent'" not in caplog.text

    def test_agent_config_allowed_tools_defaults_to_none(self):
        cfg = AgentConfig(name="atlas")
        assert cfg.allowed_tools is None

    def test_agent_config_parses_allowed_tools(self):
        cfg = AgentConfig.model_validate({"name": "scoped", "allowed_tools": ["bash"]})
        assert cfg.allowed_tools == ["bash"]

    def test_app_config_carries_tool_policy(self):
        from deerflow.config.app_config import AppConfig

        minimal = {"sandbox": {"use": "deerflow.sandbox.local.provider:LocalSandboxProvider"}}
        cfg = AppConfig.model_validate({**minimal, "tool_policy": {"source": "agent"}})
        assert cfg.tool_policy.source == "agent"
        assert AppConfig.model_validate({**minimal, "tool_policy": {"source": "skills"}}).tool_policy.source == "skills"
        assert AppConfig.model_validate(minimal).tool_policy.source is None


def _assemble_tool_names(monkeypatch, *, source: str | None, agent_allowed: list[str] | None, schedule_allowed: list[str] | None) -> list[str]:
    from unittest.mock import MagicMock, create_autospec

    import deerflow.tools as tools_module
    from deerflow.agents.lead_agent import agent as lead_agent_module
    from deerflow.config.app_config import AppConfig
    from deerflow.config.model_config import ModelConfig
    from deerflow.config.sandbox_config import SandboxConfig

    app_config = AppConfig(
        models=[ModelConfig(name="safe-model", display_name="safe-model", description=None, use="langchain_openai:ChatOpenAI", model="safe-model", supports_thinking=False, supports_vision=False)],
        sandbox=SandboxConfig(use="deerflow.sandbox.local:LocalSandboxProvider"),
        tool_policy=ToolPolicyConfig(source=source),
    )

    def _named(name: str):
        tool = MagicMock()
        tool.name = name
        return tool

    agent_config = AgentConfig(name="scoped", allowed_tools=agent_allowed)
    monkeypatch.setattr(lead_agent_module, "get_app_config", lambda: app_config)
    monkeypatch.setattr(lead_agent_module, "load_agent_config", lambda *args, **kwargs: agent_config)
    monkeypatch.setattr(tools_module, "get_available_tools", lambda **kwargs: [_named(tool.name) for tool in TOOLS])
    monkeypatch.setattr(lead_agent_module, "build_middlewares", create_autospec(lead_agent_module.build_middlewares, return_value=[]))
    monkeypatch.setattr(lead_agent_module, "apply_prompt_template", lambda **kwargs: "prompt")
    monkeypatch.setattr(lead_agent_module, "create_chat_model", lambda **kwargs: object())
    monkeypatch.setattr(lead_agent_module, "create_agent", lambda **kwargs: kwargs)
    configurable = {"agent_name": "scoped", "model_name": "safe-model", "subagent_enabled": False}
    if schedule_allowed is not None:
        configurable["allowed_tools"] = schedule_allowed
    graph = lead_agent_module.unwrap_agent_graph(lead_agent_module.make_lead_agent({"configurable": configurable}))
    return [tool.name for tool in graph["tools"]]


class TestLeadAssembly:
    @pytest.mark.parametrize("source", [None, "agent", "skills"])
    def test_agent_ceiling_applies_whatever_the_source(self, monkeypatch, source):
        """The ceiling no longer depends on tool_policy.source (#58 retired)."""
        assert _assemble_tool_names(monkeypatch, source=source, agent_allowed=["read_file"], schedule_allowed=None) == ["read_file"]

    @pytest.mark.parametrize("source", [None, "skills"])
    def test_schedule_allowlist_scopes_an_unrestricted_agent_whatever_the_source(self, monkeypatch, source):
        assert _assemble_tool_names(monkeypatch, source=source, agent_allowed=None, schedule_allowed=["bash"]) == ["bash"]

    def test_unrestricted_agent_without_schedule_keeps_every_tool(self, monkeypatch):
        # update_agent is added for every custom agent and survives too.
        assert _assemble_tool_names(monkeypatch, source="agent", agent_allowed=None, schedule_allowed=None) == [*(tool.name for tool in TOOLS), "update_agent"]


# Subagents do not inherit this ceiling: their tools come from their own
# subagent config and authorization (the executor's dead
# ``_apply_skill_allowed_tools`` was removed with upstream #6935).

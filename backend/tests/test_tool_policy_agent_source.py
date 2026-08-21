"""[argus patch #53] Agent-source tool policy (tool_policy.source: "agent").

Locks the tri-state semantics of AgentConfig.allowed_tools and the dispatch
between the upstream skill-union policy and the agent-level ceiling:

- None (undeclared) means NO restriction — a whitelist nobody declared
  cannot restrict. This is the opposite reading of "None = no tools", so it
  is pinned here explicitly.
- [] (declared empty) means no tools.
- [names] means exactly those, unioned with the firing schedule's
  allowed-tools (extra_allowed, argus patch #43).
- Skill allowed-tools declarations never grant or deny under the agent
  source; a declaration outside a restrictive ceiling is logged, not granted.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

import pytest

from deerflow.config.agents_config import AgentConfig
from deerflow.config.tool_policy_config import ToolPolicyConfig
from deerflow.skills.tool_policy import (
    filter_tools_by_agent_allowed_tools,
    filter_tools_by_skill_allowed_tools,
)
from deerflow.skills.types import Skill


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
        """The load-bearing case: skills declare, but under the agent source
        an undeclared agent ceiling binds EVERYTHING (the skill union would
        have stripped atlas_update_schedule here)."""
        skills = [_make_skill("restricted", ["read_file"])]
        result = filter_tools_by_agent_allowed_tools(TOOLS, None, skills=skills)
        assert result == TOOLS
        # contrast: the upstream skill union strips to the declaration
        assert [t.name for t in filter_tools_by_skill_allowed_tools(TOOLS, skills)] == ["read_file"]

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
    def test_tool_policy_defaults_to_upstream_skills_source(self):
        assert ToolPolicyConfig().source == "skills"

    def test_tool_policy_rejects_unknown_source(self):
        with pytest.raises(ValueError):
            ToolPolicyConfig(source="everything")

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
        assert AppConfig.model_validate(minimal).tool_policy.source == "skills"


# Subagent inheritance of the parent agent's ceiling is covered in
# tests/test_subagent_executor.py (TestAgentSourceToolPolicy), which owns the
# module-mock scaffolding needed to import the real SubagentExecutor.

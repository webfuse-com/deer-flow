"""[argus] Config keys of patches dropped at the 2026-10-25 sync keep loading.

Decision d5 dropped the planner-aligned ArgusTodoMiddleware (#7/#18, #41) and
#73's adaptive reasoning. Fleet configs roll out after the image, and some
still set the old keys, so they must load and be ignored.
"""

from __future__ import annotations

import logging

import yaml
from langchain.agents.middleware import TodoListMiddleware

from deerflow.agents.lead_agent.agent import _create_todo_list_middleware
from deerflow.agents.middlewares.todo_middleware import TodoMiddleware
from deerflow.config import agents_config as agents_config_mod
from deerflow.config.agents_config import AgentConfig
from deerflow.config.app_config import AppConfig
from deerflow.config.model_config import ModelConfig
from deerflow.config.sandbox_config import SandboxConfig

# projects/_template/data/agents/glm-planner/config.yaml (also pythia, pyrrhus).
GLM_PLANNER_YAML = """\
name: glm-planner
description: "GLM-5.2 lead that plans and critiques, delegates execution to local Qwen. Use for complex codebase changes where better planning matters."
model: glm-nw
uses_planner_pipeline: true
tool_groups:
  - file:read
  - file:write
  - bash
  - web
skills:
  - critic
  - render-and-verify
"""


def _app_config(**extra) -> AppConfig:
    return AppConfig(
        models=[ModelConfig(name="m", display_name="m", description=None, use="langchain_openai:ChatOpenAI", model="m", supports_thinking=True, supports_vision=False)],
        sandbox=SandboxConfig(use="test"),
        **extra,
    )


def test_uses_planner_pipeline_loads_and_logs_once(monkeypatch, caplog):
    monkeypatch.setattr(agents_config_mod, "_planner_pipeline_ignored_logged", False)
    with caplog.at_level(logging.WARNING, logger=agents_config_mod.__name__):
        planner = AgentConfig(**yaml.safe_load(GLM_PLANNER_YAML))
        AgentConfig(**yaml.safe_load(GLM_PLANNER_YAML))
        AgentConfig(name="themis", uses_planner_pipeline=False)
    assert planner.name == "glm-planner"
    assert planner.skills == ["critic", "render-and-verify"]
    assert caplog.text.count("uses_planner_pipeline is ignored") == 1


def test_an_explicit_false_does_not_log(monkeypatch, caplog):
    monkeypatch.setattr(agents_config_mod, "_planner_pipeline_ignored_logged", False)
    with caplog.at_level(logging.WARNING, logger=agents_config_mod.__name__):
        AgentConfig(name="themis", uses_planner_pipeline=False)
    assert "uses_planner_pipeline" not in caplog.text


def test_plan_mode_uses_the_upstream_todo_middleware():
    middleware = _create_todo_list_middleware(True)
    assert type(middleware) is TodoMiddleware
    assert isinstance(middleware, TodoListMiddleware)
    assert _create_todo_list_middleware(False) is None


def test_top_level_adaptive_reasoning_block_loads_as_an_ignored_extra():
    # Atlas stacks set enabled: false; pyrrhus and projects/_template set true.
    for block in ({"enabled": False}, {"enabled": True, "routine_tools": ["read_file"]}):
        config = _app_config(adaptive_reasoning=block)
        assert config.model_extra["adaptive_reasoning"] == block

"""High-confidence automatic skill routing stays deterministic and bounded."""

from types import SimpleNamespace

from deerflow.agents.middlewares.skill_auto_routing_middleware import SkillAutoRoutingMiddleware


def _skill(name: str):
    return SimpleNamespace(name=name, enabled=True)


def _middleware(max_auto_activated: int = 2) -> SkillAutoRoutingMiddleware:
    config = SimpleNamespace(skills=SimpleNamespace(project_skill_name="project-workspace", max_auto_activated=max_auto_activated))
    return SkillAutoRoutingMiddleware(app_config=config)


def test_durable_build_routes_project_skill() -> None:
    selected = _middleware()._select("Implement the repository refactor", [_skill("project-workspace"), _skill("webfuse")])
    assert [skill.name for skill in selected] == ["project-workspace"]


def test_literal_domain_name_can_add_one_skill() -> None:
    selected = _middleware()._select("Build a project with webfuse", [_skill("project-workspace"), _skill("webfuse")])
    assert [skill.name for skill in selected] == ["project-workspace", "webfuse"]


def test_ambiguous_request_does_not_guess() -> None:
    selected = _middleware()._select("Can you help me understand this?", [_skill("project-workspace"), _skill("webfuse")])
    assert selected == []


def test_project_word_alone_is_not_enough() -> None:
    selected = _middleware()._select("What does the word project mean?", [_skill("project-workspace")])
    assert selected == []

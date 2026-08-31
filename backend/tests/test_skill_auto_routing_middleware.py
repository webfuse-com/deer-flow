"""High-confidence automatic skill routing stays deterministic and bounded."""

from types import SimpleNamespace

from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import HumanMessage

from deerflow.agents.middlewares.skill_activation_middleware import SkillActivationMiddleware
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


def test_auto_activation_says_injected_skill_must_not_be_reloaded(monkeypatch) -> None:
    skill = SimpleNamespace(
        name="project-workspace",
        skill_file="/skills/project-workspace/SKILL.md",
        get_container_file_path=lambda _root: "/mnt/skills/public/project-workspace/SKILL.md",
    )
    storage = SimpleNamespace(
        load_skills=lambda *, enabled_only: [skill],
        get_skills_root_path=lambda: "/skills",
        get_container_root=lambda: "/mnt/skills",
    )
    middleware = _middleware()
    middleware._storage = lambda: storage
    middleware._select = lambda _text, _skills: [skill]
    monkeypatch.setattr(
        SkillActivationMiddleware,
        "_read_skill_content",
        lambda *_args, **_kwargs: "# injected body",
    )
    request = ModelRequest(
        model=object(),
        messages=[HumanMessage(content="Implement the repository refactor", id="m1")],
        tools=[],
        state={"messages": []},
        runtime=SimpleNamespace(context={}),
    )

    prepared = middleware._prepare(request)

    activation = next(message for message in prepared.messages if message.name == "auto_skill_activation")
    assert "selected and loaded" in activation.content
    assert "do not call describe_skill or reread their SKILL.md" in activation.content

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


# [argus patch #130]


def _middleware_mention_only(*names: str) -> SkillAutoRoutingMiddleware:
    config = SimpleNamespace(skills=SimpleNamespace(project_skill_name="project-workspace", max_auto_activated=2, mention_only_skills=list(names)))
    return SkillAutoRoutingMiddleware(app_config=config)


def test_mention_only_skill_ignores_the_bare_word() -> None:
    mw = _middleware_mention_only("claude")
    assert mw._select("Can you add a signing claude to the availability section?", [_skill("claude")]) == []


def test_mention_only_skill_routes_on_at_mention() -> None:
    mw = _middleware_mention_only("claude")
    selected = mw._select("@claude build the reg radar pipeline", [_skill("claude")])
    assert [skill.name for skill in selected] == ["claude"]


def test_email_address_is_not_a_mention() -> None:
    mw = _middleware_mention_only("claude")
    assert mw._select("mail me at x@claude.example", [_skill("claude")]) == []


def test_unlisted_skill_keeps_bare_word_routing() -> None:
    mw = _middleware_mention_only("claude")
    selected = mw._select("Build a project with webfuse", [_skill("project-workspace"), _skill("webfuse")])
    assert [skill.name for skill in selected] == ["project-workspace", "webfuse"]


def test_framework_hint_is_not_the_routing_target() -> None:
    from langchain_core.messages import HumanMessage

    from deerflow.utils.messages import is_real_user_message

    assert is_real_user_message(HumanMessage(content="Refresh the dashboard"))
    for name in ("progress_hint", "deadline_warning", "budget_warning", "loop_warning", "forced_stop_synthesis", "auto_skill_activation"):
        assert not is_real_user_message(HumanMessage(content="[PROGRESS HINT] ...", name=name))


def test_block_stays_on_the_user_request_when_a_hint_is_appended(monkeypatch) -> None:
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from deerflow.agents.middlewares.skill_activation_middleware import SkillActivationMiddleware

    mw = _middleware()
    skill = SimpleNamespace(name="project-workspace", enabled=True, skill_file="SKILL.md", get_container_file_path=lambda root: "/mnt/skills/public/project-workspace/SKILL.md")
    storage = SimpleNamespace(load_skills=lambda enabled_only=False: [skill], get_skills_root_path=lambda: "/skills", get_container_root=lambda: "/mnt/skills")
    monkeypatch.setattr(mw, "_storage", lambda: storage)
    monkeypatch.setattr(SkillActivationMiddleware, "_read_skill_content", staticmethod(lambda *a, **k: "workflow body"))

    user = HumanMessage(content="Update the dashboard project and deploy it", id="u1")
    history = [user, AIMessage(content="", tool_calls=[{"name": "bash", "args": {}, "id": "t1"}]), ToolMessage(content="ok", tool_call_id="t1")]

    def request(messages):
        req = SimpleNamespace(messages=messages, runtime=SimpleNamespace(context={}))
        req.override = lambda messages: SimpleNamespace(messages=messages)
        return req

    first = mw._prepare(request(history)).messages
    hinted = mw._prepare(request([*history, HumanMessage(content="[PROGRESS HINT] try another way", name="progress_hint")])).messages
    assert first[0].name == "auto_skill_activation"
    assert hinted[0].name == "auto_skill_activation"
    assert hinted[: len(first)] == first

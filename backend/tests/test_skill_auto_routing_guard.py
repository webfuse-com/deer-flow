"""[argus patch #145] Skill auto routing in unattended and allowlisted runs.

- ``skills.auto_route_unattended`` (default true) false: no auto routing in
  scheduled runs.
- ``skills.auto_route_guard`` (default false): a skill named only in a negated
  sentence is not routed, a run with a tool allowlist routes only skills whose
  declared tools it has, and the routed block follows the request and defers
  to it.
- ``skills.reread_only_after_summary`` (default false): the Active skills
  reminder asks for a re-read only for skills whose body left the conversation.
"""

from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from deerflow.agents.middlewares.durable_context_middleware import DurableContextMiddleware
from deerflow.agents.middlewares.skill_activation_middleware import SkillActivationMiddleware
from deerflow.agents.middlewares.skill_auto_routing_middleware import SkillAutoRoutingMiddleware
from deerflow.agents.middlewares.skill_context import SKILL_CONTEXT_ENTRY_KEY, render_skill_context, skill_paths_in_messages
from deerflow.config.skills_config import SkillsConfig

_KNOWLEDGE = "/mnt/skills/public/knowledge-edit/SKILL.md"
_PROJECT = "/mnt/skills/public/project-workspace/SKILL.md"


def _skill(name: str, allowed_tools=None):
    return SimpleNamespace(
        name=name,
        enabled=True,
        allowed_tools=allowed_tools,
        skill_file="SKILL.md",
        get_container_file_path=lambda root, n=name: f"/mnt/skills/public/{n}/SKILL.md",
    )


def _middleware(**skills) -> SkillAutoRoutingMiddleware:
    config = SimpleNamespace(skills=SimpleNamespace(project_skill_name="project-workspace", max_auto_activated=2, **skills))
    return SkillAutoRoutingMiddleware(app_config=config)


def _names(selected) -> list[str]:
    return [skill.name for skill in selected]


def _prepare(mw, monkeypatch, text: str, skills: list, *, context: dict | None = None, tools: list | None = None):
    storage = SimpleNamespace(load_skills=lambda enabled_only=False: skills, get_skills_root_path=lambda: "/skills", get_container_root=lambda: "/mnt/skills")
    monkeypatch.setattr(mw, "_storage", lambda: storage)
    monkeypatch.setattr(SkillActivationMiddleware, "_read_skill_content", staticmethod(lambda *a, **k: "workflow body"))
    user = HumanMessage(content=text, id="u1")
    request = SimpleNamespace(messages=[user], runtime=SimpleNamespace(context=dict(context or {})), tools=tools or [])
    request.override = lambda messages: SimpleNamespace(messages=messages)
    return mw._prepare(request).messages, user


# ---------------------------------------------------------------------------
# Defaults


def test_defaults_keep_upstream_routing():
    cfg = SkillsConfig()
    assert cfg.auto_route_unattended is True
    assert cfg.auto_route_guard is False
    assert cfg.reread_only_after_summary is False
    # A negated mention still routes without the guard (upstream token match).
    assert _names(_middleware()._select("Never write these files under /mnt/knowledge; edit only the dashboard", [_skill("knowledge-edit")])) == ["knowledge-edit"]


def test_default_placement_and_wording_are_upstream(monkeypatch):
    messages, user = _prepare(_middleware(), monkeypatch, "Use knowledge-edit to record the decision", [_skill("knowledge-edit")])
    assert messages[0].name == "auto_skill_activation" and messages[1] is user
    assert "High-confidence routing selected these workflows. Follow them in order" in messages[0].content


# ---------------------------------------------------------------------------
# Negation


def test_negated_mentions_do_not_route():
    mw = _middleware(auto_route_guard=True)
    skills = [_skill("knowledge-edit"), _skill("slack-send")]
    for text in (
        "Never write these files under /mnt/knowledge; edit only the dashboard",  # atlas-ashish
        "Do not post with slack-send unless today is a release day",
        "Don't use slack-send here",
        "We don’t need slack-send today",
        "Gebruik geen slack-send vandaag",
    ):
        assert _names(mw._select(text, skills)) == [], text


def test_affirmed_mentions_still_route():
    mw = _middleware(auto_route_guard=True)
    assert _names(mw._select("Use knowledge-edit to record the decision", [_skill("knowledge-edit")])) == ["knowledge-edit"]
    # The negation only covers what follows it in its own sentence.
    text = "Send the summary with slack-send. Never post on weekends."
    assert _names(mw._select(text, [_skill("slack-send")])) == ["slack-send"]


# ---------------------------------------------------------------------------
# Tool allowlist


def test_allowlisted_run_skips_skills_needing_other_tools(monkeypatch):
    """meeting-prep routed ticket-management (github_* tools it was not granted) 187 times in a week."""
    mw = _middleware(auto_route_guard=True)
    skills = [_skill("ticket-management", allowed_tools=("github_create_issue", "github_get_issue"))]
    context = {"unattended": True, "allowed_tools": ["calendar_list_events", "gmail_create_draft"]}
    messages, _ = _prepare(mw, monkeypatch, "Prepare the meeting and check ticket-management items", skills, context=context)
    assert [m.name for m in messages] == [None]


def test_allowlisted_run_routes_a_skill_whose_tools_it_has(monkeypatch):
    mw = _middleware(auto_route_guard=True)
    skills = [_skill("knowledge-edit", allowed_tools=("knowledge",)), _skill("legacy-sender")]
    context = {"unattended": True, "allowed_tools": ["pythia_query"]}
    tools = [SimpleNamespace(name="knowledge"), {"name": "present_files"}]
    messages, user = _prepare(mw, monkeypatch, "Review the knowledge-edit queue; legacy-sender is off", skills, context=context, tools=tools)
    assert messages[0] is user
    assert messages[1].name == "auto_skill_activation"
    assert messages[1].additional_kwargs["skill_auto_activation"] == ["knowledge-edit"]


def test_skill_without_a_declaration_is_not_routed_into_an_allowlisted_run():
    mw = _middleware(auto_route_guard=True)
    assert _names(mw._select("Post it with slack-send", [_skill("slack-send")], run_tool_names={"post_message"})) == []
    # No allowlist: unrestricted run, declarations are not required.
    assert _names(mw._select("Post it with slack-send", [_skill("slack-send")], run_tool_names=None)) == ["slack-send"]


def test_guard_places_routed_workflows_after_the_request(monkeypatch):
    messages, user = _prepare(_middleware(auto_route_guard=True), monkeypatch, "Use knowledge-edit to record the decision", [_skill("knowledge-edit")])
    assert messages[0] is user
    assert messages[1].name == "auto_skill_activation"
    assert "The request's own instructions, conditions and stop rules come first" in messages[1].content
    assert "Follow them in order" not in messages[1].content


# ---------------------------------------------------------------------------
# Unattended switch


def test_auto_route_unattended_false_skips_scheduled_runs_only(monkeypatch):
    mw = _middleware(auto_route_unattended=False)
    skills = [_skill("knowledge-edit")]
    scheduled, user = _prepare(mw, monkeypatch, "Use knowledge-edit to record the decision", skills, context={"unattended": True})
    assert scheduled == [user]
    interactive, _ = _prepare(mw, monkeypatch, "Use knowledge-edit to record the decision", skills, context={})
    assert interactive[0].name == "auto_skill_activation"


# ---------------------------------------------------------------------------
# Re-read wording


def _entry(path: str, name: str) -> dict:
    return {"name": name, "path": path, "description": "", "loaded_at": 0}


def test_render_is_upstream_without_in_context_paths():
    text = render_skill_context([_entry(_KNOWLEDGE, "knowledge-edit")])
    assert text.startswith("## Active skills (loaded earlier - re-read the file before applying its instructions)")


def test_render_splits_held_and_summarized_skills():
    entries = [_entry(_KNOWLEDGE, "knowledge-edit"), _entry(_PROJECT, "project-workspace")]
    text = render_skill_context(entries, in_context_paths={_KNOWLEDGE})
    held, dropped = text.split("## Active skills summarized away")
    assert "follow them without re-reading" in held and "knowledge-edit" in held and "project-workspace" not in held
    assert "re-read the file before applying" in dropped and "project-workspace" in dropped
    assert "summarized away" not in render_skill_context(entries[:1], in_context_paths={_KNOWLEDGE})


def test_skill_paths_in_messages_sees_reads_and_routed_blocks():
    messages = [
        AIMessage(content="", tool_calls=[{"name": "read_file", "args": {"path": _KNOWLEDGE}, "id": "r1"}, {"name": "read_file", "args": {"path": _PROJECT}, "id": "r2"}]),
        ToolMessage(content="---\ndescription: edit\n---\nbody", tool_call_id="r1", name="read_file"),
        ToolMessage(content="Error: file not found", tool_call_id="r2", name="read_file"),
        HumanMessage(content='<auto_skill_activation>\n<skill name="render-and-verify" path="/mnt/skills/public/render-and-verify/SKILL.md">\nbody\n</skill>\n</auto_skill_activation>', name="auto_skill_activation"),
    ]
    paths = skill_paths_in_messages(messages, skills_root="/mnt/skills", read_tool_names={"read_file"})
    assert paths == {_KNOWLEDGE, "/mnt/skills/public/render-and-verify/SKILL.md"}


def _durable_request(messages: list, skill_context: list):
    request = SimpleNamespace(messages=messages, state={"skill_context": skill_context}, runtime=SimpleNamespace(context={}))
    request.override = lambda messages: SimpleNamespace(messages=messages)
    return request


def test_durable_context_asks_for_a_reread_only_after_summary():
    read = [
        AIMessage(content="", tool_calls=[{"name": "read_file", "args": {"path": _KNOWLEDGE}, "id": "r1"}]),
        ToolMessage(content="body", tool_call_id="r1", name="read_file", additional_kwargs={SKILL_CONTEXT_ENTRY_KEY: {"path": _KNOWLEDGE, "description": ""}}),
    ]
    entries = [_entry(_KNOWLEDGE, "knowledge-edit")]
    mw = DurableContextMiddleware(skills_container_path="/mnt/skills", reread_only_after_summary=True)

    held = "\n".join(str(m.content) for m in mw._inject(_durable_request([SystemMessage(content="sys"), HumanMessage(content="go"), *read], entries)).messages)
    assert "follow them without re-reading" in held and "summarized away" not in held

    # After summarization the read is gone from the request: ask for the re-read.
    dropped = "\n".join(str(m.content) for m in mw._inject(_durable_request([SystemMessage(content="sys"), HumanMessage(content="go")], entries)).messages)
    assert "Active skills summarized away (re-read the file" in dropped

    upstream = DurableContextMiddleware(skills_container_path="/mnt/skills")
    text = "\n".join(str(m.content) for m in upstream._inject(_durable_request([SystemMessage(content="sys"), HumanMessage(content="go"), *read], entries)).messages)
    assert "loaded earlier - re-read the file before applying its instructions" in text

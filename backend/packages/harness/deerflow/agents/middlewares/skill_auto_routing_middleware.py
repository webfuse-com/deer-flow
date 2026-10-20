"""Deterministic, high-confidence skill routing for deferred discovery."""

from __future__ import annotations

import hashlib
import html
import logging
import re
from collections.abc import Awaitable, Callable
from typing import override

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import HumanMessage

from deerflow.agents.middlewares.skill_activation_middleware import SkillActivationMiddleware
from deerflow.runtime.events.catalog import MIDDLEWARE_SKILL_ACTIVATION_TAG
from deerflow.skills.storage import get_or_new_skill_storage, get_or_new_user_skill_storage
from deerflow.utils.messages import get_original_user_content_text, is_real_user_message

_ROUTED_REQUEST_KEY = "__auto_routed_skill_request"
_AUTO_CONTEXT_KEY = "skill_auto_activation"
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_DURABLE_NOUNS = frozenset({"project", "app", "extension", "connector", "repository", "repo", "runbook", "artifact"})
_DURABLE_ACTIONS = frozenset({"build", "create", "implement", "repair", "fix", "maintain", "continue", "resume", "update", "refactor", "deploy", "status", "progress"})

logger = logging.getLogger(__name__)


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


def _is_durable_work(text: str) -> bool:
    words = _tokens(text)
    return bool(words & _DURABLE_NOUNS and words & _DURABLE_ACTIONS)


# [argus patch #145] skills.auto_route_guard. A skill named only after a
# negation in its sentence is not a request for it: atlas-ashish's dashboard
# refresh says "Never write these files under /mnt/knowledge" and routed
# knowledge-edit on every run.
_CLAUSE_SPLIT_RE = re.compile(r"[.!?;:\n]+")
_NEGATION_RE = re.compile(
    r"\b(?:not|no|never|none|nothing|without|avoid|cannot|dont|nicht|niet|geen|nooit|kein|keine)\b|\b\w+n[\u2019']t\b",
    re.IGNORECASE,
)


# A negation governs the few words right after it in its phrase, not the rest
# of the sentence: "if not, update the release-desk" asks for release-desk.
_NEGATION_WINDOW = 8


def _affirmed_tokens(text: str) -> set[str]:
    """Tokens that are not within a negation's reach (the next few words of its comma-delimited phrase)."""
    affirmed: set[str] = set()
    for clause in _CLAUSE_SPLIT_RE.split(text):
        for phrase in clause.split(","):
            tokens = list(_TOKEN_RE.finditer(phrase.lower()))
            negated: set[int] = set()
            for negation in _NEGATION_RE.finditer(phrase):
                following = [index for index, token in enumerate(tokens) if token.start() >= negation.end()]
                negated.update(following[:_NEGATION_WINDOW])
            affirmed |= {token.group(0) for index, token in enumerate(tokens) if index not in negated}
    return affirmed


def _skill_tools_available(skill, tool_names: set[str]) -> bool:
    """True when every tool the skill declares is available in this run."""
    declared = getattr(skill, "allowed_tools", None)
    return declared is not None and set(declared) <= tool_names


def _run_tool_names(request: ModelRequest, run_context: dict | None) -> set[str] | None:
    """The tools a run with a tool allowlist can call, or None for an unrestricted run."""
    allowed = run_context.get("allowed_tools") if run_context is not None else None
    if not isinstance(allowed, list) or not allowed:
        return None
    names = {name for name in allowed if isinstance(name, str)}
    for tool in getattr(request, "tools", None) or ():
        name = tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", None)
        if isinstance(name, str):
            names.add(name)
    return names


class SkillAutoRoutingMiddleware(AgentMiddleware):
    """Load at most two skills when deterministic signals are unambiguous.

    The durable-project classifier is explicit. A domain skill is selected only
    when every token in its multi-token name appears in the request, or its
    single distinctive name token appears literally. Anything less certain is
    left to ``describe_skill``.
    """

    def __init__(self, *, app_config, available_skills: set[str] | None = None, user_id: str | None = None) -> None:
        super().__init__()
        self._app_config = app_config
        self._available_skills = set(available_skills) if available_skills is not None else None
        self._user_id = user_id

    def _storage(self):
        if self._user_id is not None:
            return get_or_new_user_skill_storage(self._user_id, app_config=self._app_config)
        return get_or_new_skill_storage(app_config=self._app_config)

    @staticmethod
    def _request_key(message: HumanMessage) -> str:
        if message.id:
            return str(message.id)
        text = get_original_user_content_text(message.content, message.additional_kwargs)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _select(self, text: str, skills: list, *, run_tool_names: set[str] | None = None) -> list:
        allowed = [s for s in skills if s.enabled and not (getattr(s, "required_secrets", ()) or ()) and (self._available_skills is None or s.name in self._available_skills)]
        # [argus patch #145] In a run with a tool allowlist, only skills whose
        # declared tools are all available (94% of skills routed into
        # scheduled runs in the week to 2026-10-19 needed tools the schedule
        # did not grant).
        guard = getattr(self._app_config.skills, "auto_route_guard", False) is True
        if guard and run_tool_names is not None:
            allowed = [s for s in allowed if _skill_tools_available(s, run_tool_names)]
        by_name = {s.name: s for s in allowed}
        selected: list = []
        project_name = self._app_config.skills.project_skill_name
        if _is_durable_work(text) and project_name in by_name:
            selected.append(by_name[project_name])

        words = _tokens(text)
        affirmed = _affirmed_tokens(text) if guard else words  # [argus patch #145]
        # [argus patch #130] delegate skills route on "@name" only: a bare
        # "claude" (atlas-ashish typo for "clause") injected the /claude
        # delegate skill into an unrelated chat.
        mention_only = {name.lower() for name in (getattr(self._app_config.skills, "mention_only_skills", None) or [])}
        candidates = []
        for skill in allowed:
            if skill in selected or skill.name == project_name:
                continue
            if skill.name.lower() in mention_only:
                if re.search(rf"(?<![\w@])@{re.escape(skill.name)}(?![\w-])", text, re.IGNORECASE):
                    candidates.append((len(_tokens(skill.name.replace("-", " "))) or 1, len(skill.name), skill))
                continue
            name_tokens = _tokens(skill.name.replace("-", " "))
            if not name_tokens:
                continue
            if name_tokens <= affirmed and (len(name_tokens) > 1 or len(next(iter(name_tokens))) >= 5):
                candidates.append((len(name_tokens), len(skill.name), skill))
        if candidates and len(selected) < self._app_config.skills.max_auto_activated:
            candidates.sort(key=lambda item: (-item[0], -item[1], item[2].name))
            selected.append(candidates[0][2])
        return selected[: self._app_config.skills.max_auto_activated]

    def _prepare(self, request: ModelRequest) -> ModelRequest:
        messages = list(request.messages)
        target_index = next((i for i in range(len(messages) - 1, -1, -1) if is_real_user_message(messages[i])), None)
        if target_index is None:
            return request
        target = messages[target_index]
        text = get_original_user_content_text(target.content, target.additional_kwargs)
        if text.lstrip().startswith("/"):
            return request
        context = getattr(request.runtime, "context", None)
        run_context = context if isinstance(context, dict) else None
        # [argus patch #145] skills.auto_route_unattended / skills.auto_route_guard
        skills_config = self._app_config.skills
        if run_context is not None and run_context.get("unattended") and getattr(skills_config, "auto_route_unattended", True) is False:
            return request
        guard = getattr(skills_config, "auto_route_guard", False) is True
        request_key = self._request_key(target)
        if run_context is not None and run_context.get(_ROUTED_REQUEST_KEY) == request_key:
            return request

        storage = self._storage()
        selected = self._select(text, storage.load_skills(enabled_only=False), run_tool_names=_run_tool_names(request, run_context) if guard else None)
        if run_context is not None:
            run_context[_ROUTED_REQUEST_KEY] = request_key
        if not selected:
            return request

        blocks = []
        routed_names = []
        for skill in selected:
            try:
                content = SkillActivationMiddleware._read_skill_content(
                    skill.skill_file,
                    storage.get_skills_root_path(),
                    storage=storage,
                )
            except (OSError, ValueError):
                logger.warning("Could not safely auto-route skill %s", skill.name, exc_info=True)
                continue
            routed_names.append(skill.name)
            blocks.append(f'<skill name="{html.escape(skill.name, quote=True)}" path="{html.escape(skill.get_container_file_path(storage.get_container_root()), quote=True)}">\n{html.escape(content, quote=False)}\n</skill>')
        if not blocks:
            return request
        # [argus patch #145] Under the guard the routed workflows follow the
        # request and defer to it: a schedule's own date gate and stop rule sat
        # below "Follow them in order" (atlas-tony Release Desk posted to a team
        # Slack channel on days it should have done nothing).
        lead = (
            "Routing matched these workflows to the request above. The request's own instructions, conditions and stop rules come first; apply a workflow only where the request calls for it, with the tools you have.\n"
            if guard
            else "High-confidence routing selected these workflows. Follow them in order; load referenced resources only when needed.\n"
        )
        reminder = HumanMessage(
            content="<auto_skill_activation>\n" + lead + "\n".join(blocks) + "\n</auto_skill_activation>",
            name="auto_skill_activation",
            additional_kwargs={_AUTO_CONTEXT_KEY: routed_names},
        )
        messages.insert(target_index + 1 if guard else target_index, reminder)

        journal = run_context.get("__run_journal") if run_context is not None else None
        if journal is not None:
            try:
                journal.record_middleware(
                    MIDDLEWARE_SKILL_ACTIVATION_TAG,
                    name="SkillAutoRoutingMiddleware",
                    hook="wrap_model_call",
                    action="auto_route",
                    changes={"skills": routed_names, "strategy": "high_confidence"},
                )
            except Exception:
                # Telemetry must never turn successful routing into a failed run.
                logger.debug("Could not record automatic skill routing", exc_info=True)
        return request.override(messages=messages)

    @override
    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]) -> ModelCallResult:
        return handler(self._prepare(request))

    @override
    async def awrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]) -> ModelCallResult:
        return await handler(self._prepare(request))

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

    def _select(self, text: str, skills: list) -> list:
        allowed = [s for s in skills if s.enabled and not (getattr(s, "required_secrets", ()) or ()) and (self._available_skills is None or s.name in self._available_skills)]
        by_name = {s.name: s for s in allowed}
        selected: list = []
        project_name = self._app_config.skills.project_skill_name
        if _is_durable_work(text) and project_name in by_name:
            selected.append(by_name[project_name])

        words = _tokens(text)
        candidates = []
        for skill in allowed:
            if skill in selected or skill.name == project_name:
                continue
            name_tokens = _tokens(skill.name.replace("-", " "))
            if not name_tokens:
                continue
            if name_tokens <= words and (len(name_tokens) > 1 or len(next(iter(name_tokens))) >= 5):
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
        request_key = self._request_key(target)
        if run_context is not None and run_context.get(_ROUTED_REQUEST_KEY) == request_key:
            return request

        storage = self._storage()
        selected = self._select(text, storage.load_skills(enabled_only=False))
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
        reminder = HumanMessage(
            content="<auto_skill_activation>\nHigh-confidence routing selected these workflows. Follow them in order; load referenced resources only when needed.\n" + "\n".join(blocks) + "\n</auto_skill_activation>",
            name="auto_skill_activation",
            additional_kwargs={_AUTO_CONTEXT_KEY: routed_names},
        )
        messages.insert(target_index, reminder)

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

"""Shared interaction policy for lead-agent tools and prompt guidance."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

ASK_CLARIFICATION_TOOL_NAME = "ask_clarification"


class RunInteractionMode(StrEnum):
    """Interaction modes that can be selected by a trusted run entry point."""

    INTERACTIVE = "interactive"
    AUTONOMOUS = "autonomous"
    WEBHOOK = "webhook"
    SCHEDULED = "scheduled"


# [argus] Interactive wording is the fork's compact "ask only when it
# materially changes the result" posture (lead prompt trim, fork PR #49), not
# upstream's ask-first block; the autonomous variants below stay upstream's.
_INTERACTIVE_CLARIFICATION_SYSTEM = """<clarification_system>
Call `ask_clarification` before action only for a missing required input, a materially different product choice, or confirmation of a destructive or external effect.
Ask one focused question with useful options when possible. The tool pauses execution; wait for the reply.
Do not ask merely because several safe implementation details are possible. Do not call any other tool in the same turn as `ask_clarification`; sibling calls are dropped.
</clarification_system>"""

_AUTONOMOUS_CLARIFICATION_SYSTEM = """<clarification_system>
**WORKFLOW PRIORITY: ASSESS -> CHOOSE THE LOWEST-RISK PATH -> ACT**

There is no human available to answer a synchronous question during this run.
Do not wait for clarification or approval. Resolve ambiguity from the request,
{context_sources}.

- For low-risk and reversible work, make the smallest reasonable assumption and continue.
- State every material assumption in the final result.
- For high-risk or irreversible work without sufficient authorization, do not guess:
  stop with a concise structured `BLOCKED` result that names the missing decision.
- Prefer inspection and read-only checks before changing state.
</clarification_system>"""

_AUTONOMOUS_CONTEXT_SOURCES = "the available run context and existing configuration"
_WEBHOOK_CONTEXT_SOURCES = "the issue, pull request, repository, event context, and existing configuration"


@dataclass(frozen=True, slots=True)
class RunInteractionPolicy:
    """The single source for interaction-sensitive tools and prompt guidance."""

    mode: RunInteractionMode

    @property
    def allows_clarification(self) -> bool:
        return self.mode is RunInteractionMode.INTERACTIVE

    @property
    def disabled_tool_names(self) -> frozenset[str]:
        if self.allows_clarification:
            return frozenset()
        return frozenset({ASK_CLARIFICATION_TOOL_NAME})

    @property
    def thinking_guidance(self) -> str:
        if self.allows_clarification:
            return "- Ask only when missing information would materially change the result; otherwise make a safe, stated assumption."
        return "- **INTERACTION CHECK: This run has no synchronous human. Resolve ambiguity with the run context, choose the lowest-risk reversible path, and record material assumptions.**"

    @property
    def clarification_system(self) -> str:
        if self.allows_clarification:
            return _INTERACTIVE_CLARIFICATION_SYSTEM
        context_sources = _WEBHOOK_CONTEXT_SOURCES if self.mode is RunInteractionMode.WEBHOOK else _AUTONOMOUS_CONTEXT_SOURCES
        return _AUTONOMOUS_CLARIFICATION_SYSTEM.format(context_sources=context_sources)

    @property
    def clarification_reminder(self) -> str:
        if self.allows_clarification:
            return "- Clarify only material missing inputs, product choices, or risky effects; use safe assumptions for routine details."
        return "- **Autonomous Interaction**: Do not wait for a human response; make minimal reversible assumptions, list them, or return a structured `BLOCKED` result for high-risk ambiguity"

    @classmethod
    def interactive(cls) -> RunInteractionPolicy:
        return cls(RunInteractionMode.INTERACTIVE)


def resolve_run_interaction_policy(config: Mapping[str, Any] | None) -> RunInteractionPolicy:
    """Resolve one policy from legacy runtime flags and channel context.

    ``non_interactive`` is the scheduler's trusted legacy flag. GitHub and
    other webhook channels currently use ``disable_clarification`` and/or
    ``channel_name``; both remain supported while callers migrate to an
    explicit mode.
    """

    merged: dict[str, Any] = {}
    if config:
        configurable = config.get("configurable")
        context = config.get("context")
        if isinstance(configurable, Mapping):
            merged.update(configurable)
        if isinstance(context, Mapping):
            merged.update(context)

    raw_mode = merged.get("interaction_mode")
    if raw_mode is not None:
        try:
            return RunInteractionPolicy(RunInteractionMode(str(raw_mode)))
        except ValueError as exc:
            valid_modes = ", ".join(mode.value for mode in RunInteractionMode)
            raise ValueError(f"Invalid interaction_mode {raw_mode!r}; expected one of: {valid_modes}") from exc

    if merged.get("non_interactive"):
        return RunInteractionPolicy(RunInteractionMode.SCHEDULED)
    if merged.get("channel_name") == "github":
        return RunInteractionPolicy(RunInteractionMode.WEBHOOK)
    if merged.get("disable_clarification"):
        return RunInteractionPolicy(RunInteractionMode.AUTONOMOUS)
    return RunInteractionPolicy.interactive()

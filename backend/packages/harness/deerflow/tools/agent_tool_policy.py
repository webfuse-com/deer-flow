"""[argus patch #53/#43] The agent's tool ceiling, plus a schedule's allowed-tools.

Upstream #6935 deleted ``skills/tool_policy.py`` and ``SkillToolPolicyMiddleware``:
a skill's ``allowed-tools`` frontmatter is compatibility metadata and never
filters tools. Argus keeps one fork-owned ceiling, applied by lead-agent
assembly before authorization and deferred-tool assembly:

- ``AgentConfig.allowed_tools`` (#53), applied whenever an agent declares it;
- the firing schedule's ``allowed-tools`` (#43, ``extra_allowed``), unioned in.

``tool_policy.source`` in config.yaml no longer selects anything (see
``config/tool_policy_config.py``). Subagents do not inherit this ceiling;
their tools come from their own subagent config and authorization.
"""

import logging
from typing import Protocol

from deerflow.skills.types import Skill

logger = logging.getLogger(__name__)


class NamedTool(Protocol):
    name: str


def filter_tools_by_agent_allowed_tools[ToolT: NamedTool](
    tools: list[ToolT],
    agent_allowed: list[str] | None,
    skills: list[Skill] | None = None,
    extra_allowed: set[str] | None = None,
) -> list[ToolT]:
    """Filter ``tools`` by the agent's ``allowed_tools`` and the schedule's list.

    The agent config's ``allowed_tools`` field is the whitelist:
    - None: no restriction. A whitelist nobody declared cannot restrict.
    - []: no tools. An explicitly declared empty whitelist.
    - [names]: exactly those, unioned with the firing schedule's
      allowed-tools (``extra_allowed``, argus patch #43). When the agent
      declares None and a schedule declares a list, the schedule's list is
      the sole whitelist. That keeps unattended scheduled runs scopable
      even on an otherwise unrestricted agent.

    Skill allowed-tools declarations neither grant nor deny anything here.
    When the effective ceiling is restrictive, each enabled skill declaring
    names outside it is logged so the mismatch is visible at agent build
    time instead of surfacing as a missing tool mid-run.
    """
    allowed = set(agent_allowed) if agent_allowed is not None else None
    if extra_allowed:
        allowed = extra_allowed if allowed is None else allowed | extra_allowed
    if allowed is None:
        return tools

    for skill in skills or []:
        if not skill.allowed_tools:
            continue
        outside = set(skill.allowed_tools) - allowed
        if outside:
            logger.warning(
                "Skill %s declares tools outside the agent's allowed_tools ceiling (documentation only, not granted): %s",
                skill.name,
                sorted(outside),
            )

    return [tool for tool in tools if tool.name in allowed]

import logging
from typing import Protocol

from deerflow.skills.types import Skill

logger = logging.getLogger(__name__)


class NamedTool(Protocol):
    name: str


# Framework built-ins that remain available even when an active skill declares
# allowed-tools. They support controlled file/review/discovery workflows rather
# than extending the reviewed/activated skill's own business-tool authority.
# In particular, promotion through tool_search does not restore a tool removed
# by SkillToolPolicyMiddleware, and describe_skill only returns catalog metadata.
ALWAYS_AVAILABLE_BUILTIN_TOOL_NAMES = frozenset(
    {
        "describe_skill",
        "read_file",
        "review_skill_package",
        "tool_search",
        "view_image",
    }
)


def allowed_tool_names_for_skills(skills: list[Skill]) -> set[str] | None:
    """Return the union of explicit skill allowed-tools declarations.

    None means legacy allow-all behavior. It is returned only when no loaded
    skill declares allowed-tools. Once any skill declares the field, legacy
    skills without the field contribute no tools instead of disabling the
    explicit restrictions from other skills.
    """
    if not skills:
        return None

    allowed: set[str] = set()
    has_explicit_declaration = False
    for skill in skills:
        if skill.allowed_tools is None:
            continue
        has_explicit_declaration = True
        if not skill.allowed_tools:
            logger.info("Skill %s declared empty allowed-tools", skill.name)
        allowed.update(skill.allowed_tools)

    if not has_explicit_declaration:
        return None
    return allowed


def filter_tools_by_skill_allowed_tools[ToolT: NamedTool](
    tools: list[ToolT],
    skills: list[Skill],
    *,
    always_allowed_tool_names: set[str] | frozenset[str] = frozenset(),
    extra_allowed: set[str] | None = None,
) -> list[ToolT]:
    """Filter tools by the union of skill allowed-tools declarations.

    [argus patch #43] ``extra_allowed`` is a set of tool names declared at the
    schedule level (from the schedule frontmatter ``allowed-tools`` field). It
    is merged with the skill-based union: if no skill declares allowed-tools
    (legacy allow-all), the extra set becomes the sole whitelist; if skills do
    declare, the two sets are unioned so the schedule's tools are available
    regardless of skills.
    """
    allowed = allowed_tool_names_for_skills(skills)
    if extra_allowed:
        if allowed is None:
            allowed = extra_allowed
        else:
            allowed = allowed | extra_allowed
    if allowed is None:
        return tools

    allowed_with_framework_tools = allowed | set(always_allowed_tool_names)
    return [tool for tool in tools if tool.name in allowed_with_framework_tools]
    return [tool for tool in tools if tool.name in allowed]


def filter_tools_by_agent_allowed_tools[ToolT: NamedTool](
    tools: list[ToolT],
    agent_allowed: list[str] | None,
    skills: list[Skill] | None = None,
    extra_allowed: set[str] | None = None,
) -> list[ToolT]:
    """[argus patch #53] Agent-source tool policy (``tool_policy.source: agent``).

    The agent config's ``allowed_tools`` field is the whitelist:
    - None: no restriction — a whitelist nobody declared cannot restrict.
    - []: no tools — an explicitly declared empty whitelist.
    - [names]: exactly those, unioned with the firing schedule's
      allowed-tools (``extra_allowed``, argus patch #43). When the agent
      declares None and a schedule declares a list, the schedule's list is
      the sole whitelist — that keeps unattended scheduled runs scopable
      even on an otherwise unrestricted agent.

    Skill allowed-tools declarations neither grant nor deny anything here;
    they stay useful as documentation and as tool_search promotion hints.
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
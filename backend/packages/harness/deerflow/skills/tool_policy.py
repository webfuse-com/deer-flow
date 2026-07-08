import logging
from typing import Protocol

from deerflow.skills.types import Skill

logger = logging.getLogger(__name__)


class NamedTool(Protocol):
    name: str


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

    return [tool for tool in tools if tool.name in allowed]

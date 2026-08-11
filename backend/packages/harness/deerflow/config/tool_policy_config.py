"""[argus patch #53] Configuration for the tool-authorization policy source."""

from typing import Literal

from pydantic import BaseModel, Field


class ToolPolicyConfig(BaseModel):
    """Which layer's allowed-tools declarations gate the run's toolset.

    - "skills" (default, upstream semantics): the union of enabled skills'
      allowed-tools frontmatter is the whitelist. It falls open only when no
      enabled skill declares the field; the firing schedule's allowed-tools
      (argus patch #43) union in.
    - "agent": the agent config's ``allowed_tools`` field is the whitelist
      (None/omitted = no restriction, [] = no tools, a list = exactly those,
      unioned with the firing schedule's allowed-tools). Skill allowed-tools
      declarations become documentation: they are logged when they exceed a
      restrictive agent ceiling, but never grant or deny anything.
    """

    source: Literal["skills", "agent"] = Field(
        default="skills",
        description=("Which declarations gate tool binding: 'skills' (upstream union of skill frontmatter) or 'agent' (AgentConfig.allowed_tools)."),
    )

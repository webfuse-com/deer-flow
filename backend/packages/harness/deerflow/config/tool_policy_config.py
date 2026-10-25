"""[argus patch #53] ``tool_policy`` config section, kept so fleet configs load.

``source`` used to choose which allowed-tools declarations gate a run: the
union of skill frontmatter (``skills``, upstream) or ``AgentConfig.allowed_tools``
(``agent``). Upstream #6935 made skill ``allowed-tools`` compatibility metadata
only, so there is nothing left to choose: an agent's ``allowed_tools`` (plus a
schedule's allowed-tools, #43) applies whenever declared
(``deerflow.tools.agent_tool_policy``). Every Atlas stack sets
``source: agent``, which is that behaviour; any other value is accepted and
ignored with a one-time log.
"""

import logging

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

_IGNORED_SOURCES_LOGGED: set[str] = set()


class ToolPolicyConfig(BaseModel):
    """Accepted for compatibility; ``source`` no longer changes behaviour."""

    source: str | None = Field(
        default=None,
        description="Ignored. An agent's allowed_tools applies whenever declared; 'agent' (the fleet value) and any other value load unchanged.",
    )

    @field_validator("source")
    @classmethod
    def _log_ignored_source(cls, value: str | None) -> str | None:
        if value is not None and value != "agent" and value not in _IGNORED_SOURCES_LOGGED:
            _IGNORED_SOURCES_LOGGED.add(value)
            logger.warning(
                "tool_policy.source=%r is ignored: skill allowed-tools no longer filter tools (upstream #6935); an agent's allowed_tools applies whenever declared.",
                value,
            )
        return value

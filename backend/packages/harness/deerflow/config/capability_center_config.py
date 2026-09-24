"""[argus patch #95] Configuration for the Capability Center entry point."""

from pydantic import BaseModel, Field


class CapabilityCenterConfig(BaseModel):
    """Whether the frontend shows the Capability Center.

    Presentation only: the capabilities, plugins and managed-models routes
    keep their own gates. A deployment whose tools and MCP servers are
    managed outside the UI hides the entry point so users are not offered a
    settings page whose saves the next deploy would overwrite.
    """

    enabled: bool = Field(
        default=True,
        description="Whether the frontend shows the Capability Center sidebar entry and page.",
    )

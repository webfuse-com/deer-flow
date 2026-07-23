"""Configuration for deferred tool loading via tool_search."""

from pydantic import BaseModel, Field


class ToolSearchConfig(BaseModel):
    """Configuration for deferred tool loading via tool_search.

    When enabled, MCP tools are not loaded into the agent's context directly.
    Instead, they are listed by name in the system prompt and discoverable
    via the tool_search tool at runtime.
    """

    enabled: bool = Field(
        default=False,
        description="Defer tools and enable tool_search",
    )
    exclude: list[str] = Field(
        default_factory=list,
        description=(
            "fnmatch patterns of FINAL (server-prefixed) tool names that stay "
            "always-bound instead of deferred, e.g. ['pythia_*', 'kb_query']. "
            "For hot tools used in most turns, where the tool_search promotion "
            "round-trip would cost more latency than their schemas cost context."
        ),
    )


_tool_search_config: ToolSearchConfig | None = None


def get_tool_search_config() -> ToolSearchConfig:
    """Get the tool search config, loading from AppConfig if needed."""
    global _tool_search_config
    if _tool_search_config is None:
        _tool_search_config = ToolSearchConfig()
    return _tool_search_config


def load_tool_search_config_from_dict(data: dict) -> ToolSearchConfig:
    """Load tool search config from a dict (called during AppConfig loading)."""
    global _tool_search_config
    _tool_search_config = ToolSearchConfig.model_validate(data)
    return _tool_search_config

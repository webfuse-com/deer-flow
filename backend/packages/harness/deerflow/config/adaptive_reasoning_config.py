"""Adaptive thinking-mode configuration."""

from pydantic import BaseModel, Field


class AdaptiveReasoningConfig(BaseModel):
    enabled: bool = Field(default=False, description="Use a no-thinking model variant after successful routine tool calls.")
    routine_tools: list[str] = Field(
        default_factory=lambda: [
            "read_file",
            "workspace_inspect",
            "write_file",
            "str_replace",
            "workspace_patch",
            "grep",
            "glob",
            "ls",
            "bash",
            "write_todos",
        ],
        description="Successful tool results after which the next model call may skip extended reasoning.",
    )

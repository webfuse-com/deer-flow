"""[argus patch #125] Prompt-prefix stability for a self-hosted prefix cache."""

from pydantic import BaseModel, Field


class PromptCacheConfig(BaseModel):
    """Keep the start of the prompt identical between model calls.

    A self-hosted engine (SGLang radix cache) reuses the computed prompt up to
    the first token that differs. Anything that changes between two model
    calls of a run, early in the prompt, makes it recompute everything after
    that point.
    """

    durable_context_volatile_at_tail: bool = Field(
        default=False,
        description=(
            "Render the parts of the durable-context block that change during a run (delegation ledger, "
            "active skills, tool-artifact handles, task notes) in a second hidden data message at the end of "
            "the request instead of after the system prompt. The goal and conversation summary stay at the "
            "head. Default false matches upstream (one block after the system prompt)."
        ),
    )

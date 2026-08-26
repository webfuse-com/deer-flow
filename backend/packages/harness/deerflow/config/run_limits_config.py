"""Configuration for per-run wall-clock limits."""

from pydantic import BaseModel, Field, model_validator


class RunLimitsConfig(BaseModel):
    """Wall-clock ceiling for a single agent run.

    Complements the token budget rather than replacing it. Cumulative token
    counting grows roughly quadratically with turn count (every turn re-sends
    the context), so a token cap tight enough to catch a runaway also
    force-stops a legitimate deep exploration mid-artifact. Wall clock is flat:
    it bounds cloud spend and, more importantly, bounds how long a person waits
    before finding out the run went nowhere.
    """

    enabled: bool = Field(
        default=False,
        description="Whether to enforce a per-run wall-clock deadline.",
    )
    wall_clock_seconds: int = Field(
        default=1800,
        ge=30,
        description=(
            "Hard deadline for one run. On expiry the agent's tool calls are "
            "stripped so it produces a final answer from what it has, and the "
            "run is stamped stop_reason=time_capped. It is not killed: a "
            "partial deliverable beats a dead thread."
        ),
    )
    warn_at_seconds: int = Field(
        default=1200,
        ge=10,
        description=("Elapsed seconds after which a single wrap-up warning is injected before the hard deadline. Must be less than wall_clock_seconds."),
    )

    @model_validator(mode="after")
    def _warn_before_stop(self) -> "RunLimitsConfig":
        if self.warn_at_seconds >= self.wall_clock_seconds:
            raise ValueError(f"warn_at_seconds ({self.warn_at_seconds}) must be less than wall_clock_seconds ({self.wall_clock_seconds}); a warning that fires at or after the hard stop can never be acted on")
        return self

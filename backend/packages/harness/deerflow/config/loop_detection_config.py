"""Configuration for loop detection middleware."""

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from deerflow.config._boolean_guards import reject_boolean


class ToolFreqOverride(BaseModel):
    """Per-tool frequency threshold override.

    Can be higher or lower than the global observability thresholds. Commonly
    used to tune reporting for high-frequency tools like bash in batch workflows
    without changing loop protection on every other tool.
    """

    warn: int = Field(ge=1)
    hard_limit: int = Field(ge=1)

    @field_validator("warn", "hard_limit", mode="before")
    @classmethod
    def reject_boolean_thresholds(cls, value: object, info: ValidationInfo) -> object:
        return reject_boolean(value, info, kind="an integer")

    @model_validator(mode="after")
    def _validate(self) -> "ToolFreqOverride":
        if self.hard_limit < self.warn:
            raise ValueError("hard_limit must be >= warn")
        return self


class LoopDetectionConfig(BaseModel):
    """Configuration for repetitive tool-call loop detection."""

    enabled: bool = Field(
        default=True,
        description="Whether to enable repetitive tool-call loop detection",
    )
    warn_threshold: int = Field(
        default=3,
        ge=1,
        description="Number of identical tool-call sets before injecting a warning",
    )
    hard_limit: int = Field(
        default=5,
        ge=1,
        description="Number of identical tool-call sets before forcing a stop",
    )
    window_size: int = Field(
        default=20,
        ge=1,
        description="Number of recent tool-call sets to track per thread",
    )
    max_tracked_threads: int = Field(
        default=100,
        ge=1,
        description="Maximum number of thread/run histories to keep in memory (legacy field name)",
    )
    tool_freq_warn: int = Field(
        default=30,
        ge=1,
        description="Number of same-tool calls in the observation window before recording a frequency metric",
    )
    tool_freq_hard_limit: int = Field(
        default=50,
        ge=1,
        description="Second same-tool frequency metric threshold; it does not stop the run",
    )
    tool_freq_overrides: dict[str, ToolFreqOverride] = Field(
        default_factory=dict,
        description=("Per-tool observability thresholds for tool_freq_warn / tool_freq_hard_limit, keyed by tool name. [argus sync] Subcategory keys such as bash.inspection are retired and ignored."),
    )
    read_file_bucket_size_lines: int = Field(
        default=200,
        ge=1,
        description=(
            "[argus] DEPRECATED, ignored. Upstream now keys read_file loop detection on the exact "
            "line window (#5578), which retired the bucket this knob sized (patch #3). Kept only so "
            "stack configs that still set it keep loading; drop it from atlas-template, then here."
        ),
    )
    no_hard_stop_tools: list[str] = Field(
        default_factory=list,
        description=(
            "[argus] Tools exempt from the loop-detection identical-call hard stop (Layer 1). Warnings are still injected, but the run is never force-stopped by "
            "repeated identical calls to these tools. Per-tool frequency is observation-only and unaffected. Use for cheap idempotent query tools where a hard stop "
            "destroys more value than the loop wastes; token_budget and run_deadline remain the cost "
            "backstops. The exemption applies only when EVERY tool in a repeated call set is listed."
        ),
    )
    recoverable_retry_limit: int = Field(
        default=24,
        ge=1,
        description=(
            "[argus] Cap on identical-call repetitions that are downgraded to warnings because the "
            "tool's most recent result was a model-recoverable soft failure (no_results, not_found, "
            "permission, near-duplicate). At this count the loop detector hard-stops regardless. "
            "Bounds the quadratic context cost of an identical retry loop that ignores escalating "
            "warnings; roughly 3x a typical hard_limit. Tools in no_hard_stop_tools are exempt from "
            "this limit too."
        ),
    )

    fresh_result_extension_tools: list[str] = Field(
        default_factory=list,
        description="[argus sync] DEPRECATED, ignored (patch #100 retired: upstream's per-tool frequency is observation-only). Kept so stack configs that set it keep loading.",
    )
    fresh_result_extension_factor: float = Field(
        default=2.0,
        ge=1.0,
        le=10.0,
        description="[argus sync] DEPRECATED, ignored (patch #100 retired).",
    )

    @field_validator(
        "warn_threshold",
        "hard_limit",
        "window_size",
        "max_tracked_threads",
        "tool_freq_warn",
        "tool_freq_hard_limit",
        mode="before",
    )
    @classmethod
    def reject_boolean_thresholds(cls, value: object, info: ValidationInfo) -> object:
        return reject_boolean(value, info, kind="an integer")

    @model_validator(mode="after")
    def validate_thresholds(self) -> "LoopDetectionConfig":
        """Keep thresholds ordered for predictable metric reporting."""
        if self.hard_limit < self.warn_threshold:
            raise ValueError("hard_limit must be greater than or equal to warn_threshold")
        if self.tool_freq_hard_limit < self.tool_freq_warn:
            raise ValueError("tool_freq_hard_limit must be greater than or equal to tool_freq_warn")
        return self

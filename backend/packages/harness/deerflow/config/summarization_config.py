"""Configuration for conversation summarization."""

from typing import Literal

from pydantic import BaseModel, Field

ContextSizeType = Literal["fraction", "tokens", "messages"]
DEFAULT_SKILL_FILE_READ_TOOL_NAMES: tuple[str, ...] = ("read_file", "read", "view", "cat")


class ContextSize(BaseModel):
    """Context size specification for trigger or keep parameters."""

    type: ContextSizeType = Field(description="Type of context size specification")
    value: int | float = Field(description="Value for the context size specification")

    def to_tuple(self) -> tuple[ContextSizeType, int | float]:
        """Convert to tuple format expected by SummarizationMiddleware."""
        return (self.type, self.value)


class SummarizationOverride(BaseModel):
    """[argus] patch #47: per-lead-model summarization overrides.

    The summarization block is global, but both the right trigger and the right
    summarizer depend on the LEAD model's context window:

    - A single absolute ``tokens`` trigger tuned for a 131k model fires at ~9%
      of a 1M-context model's window, discarding most of the context the
      operator is paying for.
    - A 131k-context summarizer cannot read a 1M-context thread it is asked to
      compress, so the compression itself is lossy in a way nobody sees.

    Only fields set here override the global block; the rest are inherited, so
    an override can adjust just the trigger.
    """

    model_name: str | None = Field(
        default=None,
        description="Summarizer model for this lead model (None = inherit the global setting)",
    )
    trigger: ContextSize | list[ContextSize] | None = Field(
        default=None,
        description="Trigger threshold(s) for this lead model (None = inherit the global setting)",
    )
    keep: ContextSize | None = Field(
        default=None,
        description="Retention policy for this lead model (None = inherit the global setting)",
    )
    trim_tokens_to_summarize: int | None = Field(
        default=None,
        description="Trim budget for this lead model (None = inherit the global setting)",
    )


class SummarizationConfig(BaseModel):
    """Configuration for automatic conversation summarization."""

    enabled: bool = Field(
        default=False,
        description="Whether to enable automatic conversation summarization",
    )
    model_name: str | None = Field(
        default=None,
        description="Model name to use for summarization. None = summarize with the model the run "
        "actually executes with (the lead run's model, a subagent's own model, or a thread's "
        "custom-agent model), not config.models[0]. When set, that model generates and the run's "
        "own model is used as a fallback if the configured summary provider fails.",
    )
    per_model: dict[str, SummarizationOverride] = Field(
        default_factory=dict,
        description="[argus] Per-lead-model overrides, keyed by the lead model's config name "
        "(e.g. 'glm-nw'). An absent key uses the global settings, so this is fully "
        "back-compatible. Use it to scale the trigger to each model's context window and "
        "to summarize a large-context thread with a large-context model.",
    )
    trigger: ContextSize | list[ContextSize] | None = Field(
        default=None,
        description="One or more thresholds that trigger summarization. When any threshold is met, summarization runs. "
        "Examples: {'type': 'messages', 'value': 50} triggers at 50 messages, "
        "{'type': 'tokens', 'value': 4000} triggers at 4000 tokens, "
        "{'type': 'fraction', 'value': 0.8} triggers at 80% of model's max input tokens",
    )
    keep: ContextSize = Field(
        default_factory=lambda: ContextSize(type="messages", value=20),
        description="Context retention policy after summarization. Specifies how much history to preserve. "
        "Examples: {'type': 'messages', 'value': 20} keeps 20 messages, "
        "{'type': 'tokens', 'value': 3000} keeps 3000 tokens, "
        "{'type': 'fraction', 'value': 0.3} keeps 30% of model's max input tokens",
    )
    trim_tokens_to_summarize: int | None = Field(
        default=4000,
        description="Maximum tokens to keep when preparing messages for summarization. Pass null to skip trimming.",
    )
    summary_prompt: str | None = Field(
        default=None,
        description="Custom prompt template for generating summaries. If not provided, uses DeerFlow's recursive execution-ledger prompt.",
    )
    skill_file_read_tool_names: list[str] = Field(
        default_factory=lambda: list(DEFAULT_SKILL_FILE_READ_TOOL_NAMES),
        description="Tool names treated as skill-file reads when capturing loaded skills into the durable skill_context channel.",
    )

    def resolved_for(self, lead_model_name: str | None) -> "SummarizationConfig":
        """[argus] patch #47: apply this lead model's ``per_model`` override.

        Returns ``self`` unchanged when there is no applicable override, so the
        common path allocates nothing and behaves exactly as before the patch.
        Never mutates the receiver.
        """
        if not lead_model_name:
            return self
        override = self.per_model.get(lead_model_name)
        if override is None:
            return self
        # Take the field VALUES, not model_dump(): dumping converts the nested
        # ContextSize models into plain dicts, and model_copy would then inject
        # dicts where the middleware factory calls `.to_tuple()` on them —
        # AttributeError at graph-build time. Pinned by
        # test_override_applies_summarizer_and_trigger.
        patch = {name: value for name in ("model_name", "trigger", "keep", "trim_tokens_to_summarize") if (value := getattr(override, name)) is not None}
        if not patch:
            return self
        # per_model is dropped from the resolved view: it has already been
        # applied, and carrying it would invite a second, recursive resolution.
        return self.model_copy(update={**patch, "per_model": {}})


# Global configuration instance
_summarization_config: SummarizationConfig = SummarizationConfig()


def get_summarization_config() -> SummarizationConfig:
    """Get the current summarization configuration."""
    return _summarization_config


def set_summarization_config(config: SummarizationConfig) -> None:
    """Set the summarization configuration."""
    global _summarization_config
    _summarization_config = config


def load_summarization_config_from_dict(config_dict: dict) -> None:
    """Load summarization configuration from a dictionary."""
    global _summarization_config
    _summarization_config = SummarizationConfig(**config_dict)

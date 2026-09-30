"""[argus patch #98] Configuration for the forced-stop synthesis turn."""

from pydantic import BaseModel, Field


class ForcedStopSynthesisConfig(BaseModel):
    """Whether a lead-agent hard stop gets one tool-free answer turn.

    Loop detection, the run deadline and the token budget stop a run by
    stripping the model's pending tool calls and appending a notice. The turn
    they cut short was a tool-calling turn, so its text is usually empty and
    the user receives the notice alone. When enabled, the stub is replaced by
    one more model call with tools disabled that answers from the results
    already collected. The notice is the fallback when that call fails or
    comes back blank.
    """

    enabled: bool = Field(
        default=True,
        description="Answer once more, without tools, after a loop/deadline/token hard stop.",
    )

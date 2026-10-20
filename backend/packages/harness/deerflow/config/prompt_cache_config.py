"""[argus patch #125] Prompt-prefix stability for a self-hosted prefix cache.

[argus patch #147] The tail layout is chosen per model, and the authority
contract can be emitted on every call so the head is stable from call 1.
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Any

from pydantic import BaseModel, Field


class PromptCacheConfig(BaseModel):
    """Keep the start of the prompt identical between model calls.

    A self-hosted engine (SGLang radix cache) reuses the computed prompt up to
    the first token that differs. Anything that changes between two model
    calls of a run, early in the prompt, makes it recompute everything after
    that point.

    Cloud caches behave differently ([argus patch #147]): OpenAI and Anthropic
    (under LiteLLM's last-message breakpoint) reuse a prefix only where an
    earlier request ended, so a request that drops the previous request's
    tail message is never an extension of it. The tail layout therefore helps
    a radix cache and hurts a cloud cache, and is chosen per model.
    """

    durable_context_volatile_at_tail: bool = Field(
        default=False,
        description=(
            "Render the parts of the durable-context block that change during a run (delegation ledger, "
            "active skills, tool-artifact handles, task notes) in a second hidden data message at the end of "
            "the request instead of after the system prompt, for the models matched by "
            "`volatile_tail_models`. The goal and conversation summary stay at the head. Default false matches "
            "upstream (one block after the system prompt) for every model."
        ),
    )
    volatile_tail_models: list[str] = Field(
        default_factory=lambda: ["local-qwen"],
        description=(
            "[argus patch #147] Models that get the tail layout while `durable_context_volatile_at_tail` is "
            "true, as fnmatch patterns matched against the model's config `name` and its `model` id. Other "
            "models keep the head layout, which a cloud prompt cache can extend call after call. `['*']` "
            "restores the stack-wide behaviour of patch #125; an empty list turns the tail layout off."
        ),
    )
    durable_context_contract_always: bool | None = Field(
        default=None,
        description=(
            "[argus patch #147] Emit the durable-context authority contract on every model call, not only "
            "once the first durable-context data exists. Without it the system message grows in the middle "
            "of a run and every cache, local or cloud, misses from that point. None follows "
            "`durable_context_volatile_at_tail`, so a stack that opted into cache-stable prompts gets it."
        ),
    )

    def volatile_tail_for(self, model_name: str | None, *, provider_model: str | None = None) -> bool:
        """Whether a run on ``model_name`` renders the changing parts at the tail."""
        if not self.durable_context_volatile_at_tail:
            return False
        names = [name for name in (model_name, provider_model) if isinstance(name, str) and name]
        if not names:
            # No model to match (a chain built without one): keep the
            # stack-wide switch exactly as patch #125 read it.
            return True
        return any(fnmatchcase(name, pattern) for pattern in self.volatile_tail_models for name in names)

    def contract_always(self) -> bool:
        if self.durable_context_contract_always is not None:
            return self.durable_context_contract_always
        return self.durable_context_volatile_at_tail


def durable_context_layout(app_config: Any, model_name: str | None) -> tuple[bool, bool]:
    """[argus patch #147] ``(volatile_at_tail, contract_always)`` for a chain on ``model_name``.

    Reads defensively: a stub or mock app config without a real
    ``prompt_cache`` section gets the upstream layout.
    """
    prompt_cache = getattr(app_config, "prompt_cache", None)
    if not isinstance(prompt_cache, PromptCacheConfig):
        return False, False
    provider_model = None
    if model_name:
        try:
            model_config = app_config.get_model_config(model_name)
        except Exception:  # noqa: BLE001 - layout must never break agent assembly
            model_config = None
        candidate = getattr(model_config, "model", None)
        provider_model = candidate if isinstance(candidate, str) else None
    return prompt_cache.volatile_tail_for(model_name, provider_model=provider_model), prompt_cache.contract_always()

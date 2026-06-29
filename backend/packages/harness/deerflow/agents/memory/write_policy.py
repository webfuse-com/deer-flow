"""[argus patch #30] Per-turn memory write policy (§4b).

A turn's memory-write decision is read from the run context, set by the channel
manager. Two independent paths enqueue long-term-memory updates — the
post-turn ``MemoryMiddleware.after_agent`` and the pre-compression
``memory_flush_hook`` (summarization) — so the policy MUST be enforced in both,
or an unattended (scheduled) turn that happens to summarize would still write.

Policy:
- ``memory_mode`` ∈ {off, read-only, read-write} on the run context wins.
- An unattended turn with no explicit mode defaults to read-only.
- Only ``read-write`` (or a normal attended turn with no mode set) writes.

Interactive turns carry neither key and therefore always write, unchanged.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _runtime_context(runtime: Any) -> dict[str, Any]:
    ctx = getattr(runtime, "context", None)
    if isinstance(ctx, dict):
        return ctx
    # Runtime.context may be a mapping-like or attribute container; normalize to
    # something with .get, falling back to an empty dict.
    if ctx is not None and hasattr(ctx, "get"):
        return ctx  # type: ignore[return-value]
    return {}


def effective_memory_mode(runtime: Any) -> str | None:
    """Resolve the per-turn memory mode, or None for an ordinary attended turn."""
    ctx = _runtime_context(runtime)
    mode = ctx.get("memory_mode") if hasattr(ctx, "get") else None
    if mode is None and (ctx.get("unattended") if hasattr(ctx, "get") else False):
        mode = "read-only"
    return mode


def memory_write_allowed(runtime: Any) -> bool:
    """Return True when this turn may write long-term memory.

    False for read-only/off (e.g. a scheduled playbook turn); True for
    read-write and for ordinary interactive turns (no mode set).
    """
    mode = effective_memory_mode(runtime)
    if mode is not None and mode != "read-write":
        logger.info("[patch #30] memory write skipped: memory_mode=%s", mode)
        return False
    return True

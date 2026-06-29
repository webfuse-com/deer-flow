"""Summarization-driven memory queue flush hook."""

from __future__ import annotations

from deerflow.agents.memory import get_memory_manager
from deerflow.agents.memory.write_policy import memory_write_allowed
from deerflow.agents.middlewares.summarization_middleware import SummarizationEvent
from deerflow.config.memory_config import get_memory_config
from deerflow.runtime.user_context import resolve_runtime_user_id


def memory_flush_hook(event: SummarizationEvent) -> None:
    """Flush messages about to be summarized into the memory queue.

    Thin, backend-agnostic entry: only the ``enabled`` + ``thread_id`` gate
    and ``user_id`` resolution live here. The backend (via
    ``manager.add_nowait``) does the filtering, human/AI validation, and
    correction/reinforcement detection.
    """
    if not get_memory_config().enabled or not event.thread_id:
        return
    # [argus patch #30] Honor the per-turn memory policy (§4b) here too — this
    # pre-compression flush is a SECOND write path alongside
    # MemoryMiddleware.after_agent. Without this gate an unattended (read-only)
    # job turn that grows long enough to summarize would still write memory.
    if not memory_write_allowed(event.runtime):
        return

    user_id = resolve_runtime_user_id(event.runtime)
    get_memory_manager().add_nowait(
        event.thread_id,
        list(event.messages_to_summarize),
        agent_name=event.agent_name,
        user_id=user_id,
    )

import logging

from deerflow.runtime.events.store.base import RunEventStore
from deerflow.runtime.events.store.memory import MemoryRunEventStore

logger = logging.getLogger(__name__)


def make_run_event_store(config=None) -> RunEventStore:
    """Create a RunEventStore based on run_events.backend configuration."""
    if config is None or config.backend == "memory":
        return MemoryRunEventStore()
    if config.backend == "db":
        from deerflow.persistence.engine import get_session_factory

        sf = get_session_factory()
        if sf is None:
            # database.backend=memory but run_events.backend=db -> fallback.
            # Keep the compatibility fallback, but make the loss of durable
            # run history impossible to miss in gateway logs.
            logger.error("run_events.backend=db but no SQL session factory is configured; falling back to in-memory run events, which are lost on restart")
            return MemoryRunEventStore()
        from deerflow.runtime.events.store.db import DbRunEventStore

        return DbRunEventStore(sf, max_trace_content=config.max_trace_content)
    if config.backend == "jsonl":
        from deerflow.runtime.events.store.jsonl import JsonlRunEventStore

        return JsonlRunEventStore()
    raise ValueError(f"Unknown run_events backend: {config.backend!r}")


__all__ = ["MemoryRunEventStore", "RunEventStore", "make_run_event_store"]

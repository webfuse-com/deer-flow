from __future__ import annotations

import logging
import os
from typing import Any

from deerflow.config import (
    get_enabled_tracing_providers,
    get_tracing_config,
    is_monocle_tracing_enabled,
    validate_enabled_tracing_providers,
)
from deerflow.tracing.monocle import is_monocle_setup_completed

logger = logging.getLogger(__name__)


def _create_langsmith_tracer(config) -> Any:
    from langchain_core.tracers.langchain import LangChainTracer

    return LangChainTracer(project_name=config.project)


def _create_langfuse_handler(config) -> Any:
    from langfuse import Langfuse
    from langfuse.langchain import CallbackHandler as LangfuseCallbackHandler

    from deerflow.tracing import argus

    # [argus patch #121] Bound the export queue BEFORE the client builds its
    # BatchSpanProcessor (which reads OTEL_BSP_MAX_QUEUE_SIZE): an unreachable
    # Langfuse then drops spans instead of growing a gateway capped at 2 GB.
    # The batch size must not exceed the queue, or construction fails.
    os.environ.setdefault("OTEL_BSP_MAX_QUEUE_SIZE", "512")

    # langfuse>=4 initializes project-specific credentials through the client
    # singleton; the LangChain callback then attaches to that configured client.
    # The singleton keeps the FIRST construction's settings per public key, so
    # every option must be passed here, on every call.
    Langfuse(
        secret_key=config.secret_key,
        public_key=config.public_key,
        host=config.host,
        # [argus patch #121] masking, the per-chat off switch, batching.
        mask=argus.mask,
        should_export_span=argus.should_export_span,
        flush_at=64,
        flush_interval=2,
        environment=os.environ.get("LANGFUSE_TRACING_ENVIRONMENT") or "argus",
    )
    return LangfuseCallbackHandler(public_key=config.public_key)


def build_tracing_callbacks() -> list[Any]:
    """Build callbacks for all explicitly enabled tracing providers.

    [argus patch #121] Fail-open: a provider that is enabled but incomplete, or
    whose handler fails to initialize, is logged and skipped; the run goes on
    untraced. Upstream raised, which failed every run on the stack (the same
    class as the 2026-08-02 outage, where one bad tool path killed 18 stacks).
    """
    try:
        validate_enabled_tracing_providers()
    except Exception as exc:  # noqa: BLE001 - observability must never break a run
        _warn_once("validate", f"Tracing is enabled but not fully configured; continuing without it: {exc}")
    # Monocle is not a callback provider; this per-run path is just where an
    # embedded process that skipped Gateway-lifespan setup can be told about it.
    if is_monocle_tracing_enabled() and not is_monocle_setup_completed():
        logger.debug(
            "MONOCLE_TRACING is set but Monocle is not initialized in this process — only the Gateway lifespan runs setup automatically; embedded/TUI callers must call deerflow.tracing.setup_monocle_tracing_if_enabled() themselves."
        )
    enabled_providers = get_enabled_tracing_providers()
    if not enabled_providers:
        return []

    tracing_config = get_tracing_config()
    callbacks: list[Any] = []

    for provider in enabled_providers:
        try:
            if provider == "langsmith":
                callbacks.append(_create_langsmith_tracer(tracing_config.langsmith))
            elif provider == "langfuse":
                callbacks.append(_create_langfuse_handler(tracing_config.langfuse))
        except Exception as exc:  # noqa: BLE001 - observability must never break a run
            _warn_once(f"init:{provider}", f"{provider} tracing initialization failed; continuing without it: {exc}")

    return callbacks


_warned: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    if key not in _warned:
        _warned.add(key)
        logger.warning(message)

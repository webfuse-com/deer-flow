"""Add ``aprune`` to ``langgraph.checkpoint.postgres.aio.AsyncPostgresSaver``.

LangGraph's postgres checkpointer (as of langgraph-checkpoint-postgres 3.0.x)
ships ``adelete_thread`` but not ``aprune``. ``langgraph_api`` warns about
unbounded checkpoint growth when no prune method exists, and operators have no
in-process way to keep history bounded short of dropping the whole thread.

This module installs an ``aprune`` method on the class at import time. Idempotent:
no-op if the library already provides one (so a future upstream release that adds
``aprune`` natively wins automatically and this file becomes safe to delete).

Two strategies:

- ``keep_latest`` (default) — for each ``(thread_id, checkpoint_ns)`` keep only
  the most recent ``checkpoint_id`` (ULID, lexicographically monotonic) and
  cascade the deletion to ``checkpoint_writes``. ``checkpoint_blobs`` is left
  alone because blobs are content-addressed and reused across checkpoints; per-
  checkpoint blob cleanup risks dropping rows the surviving checkpoint still
  references, mirroring how the upstream ``adelete_thread`` is the only path
  that touches blobs.
- ``delete_all`` — bulk delete across all three tables for the given threads.

Activated by importing this module from the checkpointer providers (see
``provider.py`` and ``async_provider.py``).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

try:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
except ImportError:  # pragma: no cover — package not installed in this env
    AsyncPostgresSaver = None  # type: ignore[assignment]


_PRUNE_SQL = """
WITH keep AS (
    SELECT thread_id, checkpoint_ns, MAX(checkpoint_id) AS keep_id
    FROM {table}
    WHERE thread_id = ANY(%s)
    GROUP BY thread_id, checkpoint_ns
)
DELETE FROM {table} t
USING keep k
WHERE t.thread_id = k.thread_id
  AND t.checkpoint_ns = k.checkpoint_ns
  AND t.checkpoint_id <> k.keep_id
"""


async def _aprune(
    self: Any,
    thread_ids: Sequence[str],
    *,
    strategy: str = "keep_latest",
) -> None:
    if not thread_ids:
        return

    ids: list[str] = [str(t) for t in thread_ids]

    if strategy == "delete_all":
        async with self._cursor(pipeline=True) as cur:
            for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
                await cur.execute(
                    f"DELETE FROM {table} WHERE thread_id = ANY(%s)",
                    (ids,),
                )
        return

    if strategy != "keep_latest":
        raise ValueError(f"unknown aprune strategy: {strategy!r}")

    async with self._cursor(pipeline=True) as cur:
        for table in ("checkpoints", "checkpoint_writes"):
            await cur.execute(_PRUNE_SQL.format(table=table), (ids,))


def _install() -> None:
    """Install ``aprune`` on AsyncPostgresSaver if the library doesn't already provide one."""
    if AsyncPostgresSaver is None:
        return
    if "aprune" in AsyncPostgresSaver.__dict__:
        return
    AsyncPostgresSaver.aprune = _aprune  # type: ignore[attr-defined]


_install()

"""Tests for AsyncPostgresSaver.aprune install and behavior.

The patch lives in ``deerflow.agents.checkpointer._postgres_aprune`` and is
activated by importing it from ``async_provider.py``. These tests verify:

  - ``_install()`` is idempotent.
  - ``_install()`` does not override a native ``aprune`` if upstream ever
    ships one (then the patch becomes a no-op and is safe to delete).
  - ``aprune`` issues the expected SQL for both strategies.
  - Unknown strategy raises ValueError.
  - Empty thread list is a no-op.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

# Skip the whole module when langgraph-checkpoint-postgres is not installed
# (e.g. in upstream's default test environment, which only depends on the
# sqlite + memory checkpointers). Our Argus fork installs the postgres
# checkpointer at image build time, so the test runs in our CI.
pytest.importorskip("langgraph.checkpoint.postgres.aio")

from deerflow.agents.checkpointer import _postgres_aprune  # noqa: E402, I001


# ---------------------------------------------------------------------------
# Install logic
# ---------------------------------------------------------------------------


class TestInstall:
    def test_install_is_idempotent(self):
        """Calling _install twice should not double-wrap or error."""
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        before = AsyncPostgresSaver.aprune
        _postgres_aprune._install()
        _postgres_aprune._install()
        after = AsyncPostgresSaver.aprune
        assert before is after

    def test_install_skips_when_native_method_exists(self, monkeypatch):
        """If the library ever ships a native aprune, ours must not overwrite it."""
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        sentinel = object()
        # Place a fake native method directly in the class __dict__
        original = AsyncPostgresSaver.__dict__.get("aprune")
        try:
            AsyncPostgresSaver.aprune = sentinel  # type: ignore[assignment]
            _postgres_aprune._install()
            assert AsyncPostgresSaver.aprune is sentinel
        finally:
            if original is None:
                # Restore our installed method
                _postgres_aprune._install()
            else:
                AsyncPostgresSaver.aprune = original  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# aprune behavior — exercised against a fake saver with a mocked cursor
# ---------------------------------------------------------------------------


class _FakeCursor:
    """Captures executed SQL + parameters for assertions."""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, params: tuple) -> None:
        self.calls.append((sql, params))


class _FakeSaver:
    """Just enough surface to satisfy ``aprune``: a ``_cursor`` async ctx manager."""

    def __init__(self):
        self.cursor = _FakeCursor()

    @asynccontextmanager
    async def _cursor(self, pipeline: bool = False):
        yield self.cursor


@pytest.mark.anyio
class TestApruneBehavior:
    async def test_empty_thread_list_is_noop(self):
        saver = _FakeSaver()
        await _postgres_aprune._aprune(saver, [])
        assert saver.cursor.calls == []

    async def test_keep_latest_issues_two_deletes(self):
        """keep_latest must issue DELETEs for checkpoints and checkpoint_writes,
        but NOT for checkpoint_blobs (blobs are content-addressed and shared)."""
        saver = _FakeSaver()
        await _postgres_aprune._aprune(saver, ["t1", "t2"], strategy="keep_latest")

        assert len(saver.cursor.calls) == 2
        tables_touched = ["checkpoints" if "FROM checkpoints" in sql else "checkpoint_writes" for sql, _ in saver.cursor.calls]
        assert set(tables_touched) == {"checkpoints", "checkpoint_writes"}
        # Blobs must not be touched
        for sql, _ in saver.cursor.calls:
            assert "checkpoint_blobs" not in sql

        # Each DELETE must scope to the provided thread ids
        for _, params in saver.cursor.calls:
            assert params == (["t1", "t2"],)

    async def test_delete_all_touches_all_three_tables(self):
        saver = _FakeSaver()
        await _postgres_aprune._aprune(saver, ["t1"], strategy="delete_all")

        assert len(saver.cursor.calls) == 3
        tables = {"checkpoints" if "FROM checkpoints " in sql else "checkpoint_blobs" if "FROM checkpoint_blobs " in sql else "checkpoint_writes" for sql, _ in saver.cursor.calls}
        assert tables == {"checkpoints", "checkpoint_blobs", "checkpoint_writes"}

    async def test_unknown_strategy_raises(self):
        saver = _FakeSaver()
        with pytest.raises(ValueError, match="unknown aprune strategy"):
            await _postgres_aprune._aprune(saver, ["t1"], strategy="bogus")

    async def test_thread_ids_are_stringified(self):
        """UUID objects (or anything else) should be coerced to str before the query."""
        import uuid

        saver = _FakeSaver()
        tid = uuid.uuid4()
        await _postgres_aprune._aprune(saver, [tid], strategy="keep_latest")

        for _, params in saver.cursor.calls:
            assert params == ([str(tid)],)

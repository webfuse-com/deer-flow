"""Tests for the bounded checkpointer connection pool (argus patch #39).

``_build_postgres_pool`` must construct an ELASTIC pool: psycopg_pool's
default is a fixed pool (``max_size`` defaults to ``min_size=4``), which
means every worker process permanently holds 4 idle Postgres connections.
Across many gateway deployments sharing one Postgres server that idle floor
adds up until ``max_connections`` is exhausted (observed 2026-07-01).

These tests pin the bounds (min 1 / max 4 / idle-shrink after 300s), their
env-var overrides, and that the keepalive/connection-check wiring survives.
psycopg_pool is faked via ``sys.modules`` so the suite needs no live
Postgres and no installed psycopg.
"""

from __future__ import annotations

import sys
import types

import pytest

from deerflow.runtime.checkpointer import async_provider

_ENV_VARS = (
    "DEERFLOW_CHECKPOINTER_POOL_MIN",
    "DEERFLOW_CHECKPOINTER_POOL_MAX",
    "DEERFLOW_CHECKPOINTER_POOL_MAX_IDLE",
)


class _RecordingPool:
    """Stand-in for psycopg_pool.AsyncConnectionPool that records ctor args."""

    last: _RecordingPool | None = None

    def __init__(self, conninfo, *, min_size, max_size, max_idle, kwargs, check):
        self.conninfo = conninfo
        self.min_size = min_size
        self.max_size = max_size
        self.max_idle = max_idle
        self.kwargs = kwargs
        self.check = check
        _RecordingPool.last = self

    @staticmethod
    def check_connection(conn):  # referenced as AsyncConnectionPool.check_connection
        raise AssertionError("not meant to be called in tests")


@pytest.fixture
def fake_psycopg(monkeypatch):
    pool_mod = types.ModuleType("psycopg_pool")
    pool_mod.AsyncConnectionPool = _RecordingPool

    rows_mod = types.ModuleType("psycopg.rows")
    rows_mod.dict_row = object()
    psycopg_mod = types.ModuleType("psycopg")
    psycopg_mod.rows = rows_mod

    monkeypatch.setitem(sys.modules, "psycopg_pool", pool_mod)
    monkeypatch.setitem(sys.modules, "psycopg", psycopg_mod)
    monkeypatch.setitem(sys.modules, "psycopg.rows", rows_mod)

    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    _RecordingPool.last = None
    yield rows_mod
    _RecordingPool.last = None


def test_pool_is_elastic_by_default(fake_psycopg) -> None:
    async_provider._build_postgres_pool("postgresql://x/y")

    pool = _RecordingPool.last
    assert pool is not None
    assert pool.conninfo == "postgresql://x/y"
    assert pool.min_size == 1
    assert pool.max_size == 4
    assert pool.max_idle == 300.0


def test_pool_bounds_respect_env_overrides(fake_psycopg, monkeypatch) -> None:
    monkeypatch.setenv("DEERFLOW_CHECKPOINTER_POOL_MIN", "2")
    monkeypatch.setenv("DEERFLOW_CHECKPOINTER_POOL_MAX", "8")
    monkeypatch.setenv("DEERFLOW_CHECKPOINTER_POOL_MAX_IDLE", "60")

    async_provider._build_postgres_pool("postgresql://x/y")

    pool = _RecordingPool.last
    assert pool is not None
    assert pool.min_size == 2
    assert pool.max_size == 8
    assert pool.max_idle == 60.0


def test_keepalive_and_check_wiring_preserved(fake_psycopg) -> None:
    async_provider._build_postgres_pool("postgresql://x/y")

    pool = _RecordingPool.last
    assert pool is not None
    assert pool.kwargs["autocommit"] is True
    assert pool.kwargs["prepare_threshold"] == 0
    assert pool.kwargs["row_factory"] is fake_psycopg.dict_row
    assert pool.kwargs["keepalives"] == 1
    assert pool.kwargs["keepalives_idle"] == 60
    assert pool.check is _RecordingPool.check_connection

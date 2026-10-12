"""[argus patch #124] A working thread's sandbox is reclaimed while the model thinks.

The first sandbox tool of a turn paid 3.5-5 s to reclaim the thread's parked
sandbox (atlas-nicholas, 2026-10-12). With ``sandbox.prewarm`` the reclaim
starts at run start, under the run's own lease owner, so the tool reuses the
binding and the worker's run-end fence releases it either way.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from langgraph.runtime import Runtime

from deerflow.sandbox import middleware as sandbox_middleware_module
from deerflow.sandbox.lease import SANDBOX_PREWARM_CONTEXT_KEY, get_sandbox_lease_manager, release_sandbox_execution_lease_async
from deerflow.sandbox.middleware import SandboxMiddleware
from deerflow.sandbox.sandbox_provider import SandboxProvider, reset_sandbox_provider, set_sandbox_provider


class _AsyncOnlyProvider(SandboxProvider):
    """Parks and reclaims one sandbox; never acquires synchronously."""

    def __init__(self) -> None:
        self.thread_ids: list[str | None] = []
        self.released_ids: list[str] = []
        self.sandbox = MagicMock(id="async-sandbox")

    def acquire(self, thread_id=None, *, user_id=None):
        raise AssertionError("async middleware should not call sync acquire")

    async def acquire_async(self, thread_id=None, *, user_id=None):
        self.thread_ids.append(thread_id)
        return "async-sandbox"

    def get(self, sandbox_id):
        return self.sandbox if sandbox_id == "async-sandbox" else None

    def get_scoped(self, sandbox_id, *, thread_id, user_id):
        return self.get(sandbox_id)

    def release(self, sandbox_id):
        self.released_ids.append(sandbox_id)


@pytest.fixture
def provider(monkeypatch):
    provider = _AsyncOnlyProvider()
    set_sandbox_provider(provider)

    async def no_auth(**_kwargs):
        return None

    monkeypatch.setattr(sandbox_middleware_module, "authorize_sandbox_execution_async", no_auth)
    yield provider
    reset_sandbox_provider()


def _config(monkeypatch, prewarm: bool):
    async def app_config():
        return SimpleNamespace(sandbox=SimpleNamespace(prewarm=prewarm))

    monkeypatch.setattr(sandbox_middleware_module, "safe_app_config_async", app_config)


def _runtime():
    return Runtime(context={"thread_id": "thread-w", "user_id": "owner-w"})


@pytest.mark.anyio
async def test_prewarm_binds_the_threads_sandbox_and_run_end_releases_it(provider, monkeypatch):
    _config(monkeypatch, True)
    runtime = _runtime()
    await SandboxMiddleware().abefore_agent({"sandbox": {"sandbox_id": "async-sandbox"}}, runtime)

    task = runtime.context[SANDBOX_PREWARM_CONTEXT_KEY]
    assert await task == "async-sandbox"
    assert runtime.context["sandbox_id"] == "async-sandbox"
    owner = runtime.context["sandbox_lease_owner_id"]
    manager = get_sandbox_lease_manager(provider)
    # A later tool call by the same owner reuses the binding without acquiring.
    again = await manager.reuse_or_acquire_async(owner, "async-sandbox", thread_id="thread-w", user_id="owner-w")
    assert again == "async-sandbox" and provider.thread_ids == []

    await release_sandbox_execution_lease_async(runtime.context)
    assert manager._bindings_by_owner.get(owner) is None


@pytest.mark.anyio
async def test_run_end_waits_for_a_prewarm_still_in_flight(provider, monkeypatch):
    _config(monkeypatch, True)
    runtime = _runtime()
    await SandboxMiddleware().abefore_agent({"sandbox": {"sandbox_id": "async-sandbox"}}, runtime)
    owner = runtime.context["sandbox_lease_owner_id"]

    # Release before the task had a chance to run: the fence drains it first.
    await release_sandbox_execution_lease_async(runtime.context)

    assert SANDBOX_PREWARM_CONTEXT_KEY not in runtime.context
    assert get_sandbox_lease_manager(provider)._bindings_by_owner.get(owner) is None


@pytest.mark.anyio
async def test_no_prewarm_when_switched_off(provider, monkeypatch):
    _config(monkeypatch, False)
    runtime = _runtime()
    await SandboxMiddleware().abefore_agent({"sandbox": {"sandbox_id": "async-sandbox"}}, runtime)
    assert SANDBOX_PREWARM_CONTEXT_KEY not in runtime.context


@pytest.mark.anyio
async def test_no_prewarm_for_a_thread_that_never_had_a_sandbox(provider, monkeypatch):
    _config(monkeypatch, True)
    runtime = _runtime()
    await SandboxMiddleware().abefore_agent({}, runtime)
    assert SANDBOX_PREWARM_CONTEXT_KEY not in runtime.context
    assert provider.thread_ids == []


@pytest.mark.anyio
async def test_a_failed_prewarm_leaves_the_lazy_path(provider, monkeypatch):
    _config(monkeypatch, True)

    async def deny(**_kwargs):
        raise RuntimeError("sandbox:execute denied")

    monkeypatch.setattr(sandbox_middleware_module, "authorize_sandbox_execution_async", deny)
    runtime = _runtime()
    await SandboxMiddleware().abefore_agent({"sandbox": {"sandbox_id": "async-sandbox"}}, runtime)
    assert await runtime.context[SANDBOX_PREWARM_CONTEXT_KEY] is None
    assert "sandbox_id" not in runtime.context

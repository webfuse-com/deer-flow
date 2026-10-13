"""[argus patch #132] A playbook fire on a new thread prewarms its sandbox too.

With ``history: none`` every fire starts a new thread, so #124 (which needs a
sandbox already named in the checkpoint) never fired and each fire paid
3.4-3.7 s on its first file or shell call (atlas-ashish, 2026-10-12/13).
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


class _Provider(SandboxProvider):
    def __init__(self) -> None:
        self.thread_ids: list[str | None] = []
        self.released_ids: list[str] = []
        self.sandbox = MagicMock(id="fresh-sandbox")

    def acquire(self, thread_id=None, *, user_id=None):
        raise AssertionError("async middleware should not call sync acquire")

    async def acquire_async(self, thread_id=None, *, user_id=None):
        self.thread_ids.append(thread_id)
        return "fresh-sandbox"

    def get(self, sandbox_id):
        return self.sandbox if sandbox_id == "fresh-sandbox" else None

    def get_scoped(self, sandbox_id, *, thread_id, user_id):
        return self.get(sandbox_id)

    def release(self, sandbox_id):
        self.released_ids.append(sandbox_id)


@pytest.fixture
def provider(monkeypatch):
    provider = _Provider()
    set_sandbox_provider(provider)

    async def no_auth(**_kwargs):
        return None

    monkeypatch.setattr(sandbox_middleware_module, "authorize_sandbox_execution_async", no_auth)
    yield provider
    reset_sandbox_provider()


def _config(monkeypatch, *, prewarm=False, playbook=True):
    async def app_config():
        return SimpleNamespace(sandbox=SimpleNamespace(prewarm=prewarm, prewarm_playbook_runs=playbook))

    monkeypatch.setattr(sandbox_middleware_module, "safe_app_config_async", app_config)


def _runtime(**context):
    return Runtime(context={"thread_id": "thread-new", "user_id": "owner-p", **context})


@pytest.mark.anyio
async def test_playbook_fire_with_sandbox_tools_prewarms_a_new_thread(provider, monkeypatch):
    _config(monkeypatch)
    runtime = _runtime(unattended=True, allowed_tools=["read_file", "bash", "bq_query"])
    await SandboxMiddleware().abefore_agent({}, runtime)

    assert await runtime.context[SANDBOX_PREWARM_CONTEXT_KEY] == "fresh-sandbox"
    assert provider.thread_ids == ["thread-new"]
    owner = runtime.context["sandbox_lease_owner_id"]
    manager = get_sandbox_lease_manager(provider)
    # the first tool's acquire finds this owner's binding and does not acquire again
    assert await manager.acquire_async(owner, "thread-new", user_id="owner-p") == "fresh-sandbox"
    assert provider.thread_ids == ["thread-new"]

    await release_sandbox_execution_lease_async(runtime.context)
    assert manager._bindings_by_owner.get(owner) is None


@pytest.mark.anyio
async def test_off_by_default(provider, monkeypatch):
    _config(monkeypatch, playbook=False)
    runtime = _runtime(unattended=True, allowed_tools=["bash"])
    await SandboxMiddleware().abefore_agent({}, runtime)
    assert SANDBOX_PREWARM_CONTEXT_KEY not in runtime.context


@pytest.mark.anyio
async def test_playbook_without_sandbox_tools_stays_lazy(provider, monkeypatch):
    _config(monkeypatch)
    runtime = _runtime(unattended=True, allowed_tools=["gmail_list_threads", "pythia_query"])
    await SandboxMiddleware().abefore_agent({}, runtime)
    assert SANDBOX_PREWARM_CONTEXT_KEY not in runtime.context
    assert provider.thread_ids == []


@pytest.mark.anyio
async def test_interactive_new_thread_stays_lazy(provider, monkeypatch):
    _config(monkeypatch, prewarm=True)
    runtime = _runtime(allowed_tools=["bash"])
    await SandboxMiddleware().abefore_agent({}, runtime)
    assert SANDBOX_PREWARM_CONTEXT_KEY not in runtime.context


@pytest.mark.anyio
async def test_existing_thread_still_needs_the_124_switch(provider, monkeypatch):
    _config(monkeypatch, prewarm=False, playbook=True)
    runtime = _runtime(unattended=True, allowed_tools=["bash"])
    await SandboxMiddleware().abefore_agent({"sandbox": {"sandbox_id": "fresh-sandbox"}}, runtime)
    assert SANDBOX_PREWARM_CONTEXT_KEY not in runtime.context

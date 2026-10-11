"""[argus patch #111] Deployment-supplied managed subagent store; managed thinking_enabled."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.gateway.routers import subagents as router
from deerflow.config.agent_storage_config import AgentStorageConfig
from deerflow.config.app_config import AppConfig, reset_app_config, set_app_config
from deerflow.config.sandbox_config import SandboxConfig
from deerflow.persistence import managed_subagents as pkg
from deerflow.persistence.agents.base import AgentStoreError
from deerflow.persistence.managed_subagents import ManagedSubagentDefinition, make_managed_subagent_store
from deerflow.persistence.managed_subagents.file import FileManagedSubagentStore

pytestmark = pytest.mark.asyncio


class RepoStore(FileManagedSubagentStore):
    refuse: AgentStoreError | None = None

    def source_info(self, name):
        return {"kind": "github", "url": f"https://github.com/acme/agents/blob/main/subagents/{name}.md"}

    def update(self, definition):
        if RepoStore.refuse:
            raise RepoStore.refuse
        super().update(definition)

    def delete(self, name):
        if RepoStore.refuse:
            raise RepoStore.refuse
        return super().delete(name)


def _request(role="admin"):
    return SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(system_role=role)))


@pytest.fixture(autouse=True)
def _environment(tmp_path, monkeypatch):
    monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path))
    monkeypatch.setattr("deerflow.config.paths._paths", None)
    set_app_config(AppConfig(sandbox=SandboxConfig(use="deerflow.sandbox.local:LocalSandboxProvider")))
    RepoStore.refuse = None
    yield
    reset_app_config()


async def test_subagent_store_class_selects_one_instance():
    pkg._custom_stores.clear()
    config = SimpleNamespace(
        agent_storage=AgentStorageConfig(subagent_store_class="test_subagent_store_class:RepoStore"),
        database=SimpleNamespace(backend="memory"),
    )
    first = make_managed_subagent_store(config)
    assert isinstance(first, RepoStore) and make_managed_subagent_store(config) is first
    plain = SimpleNamespace(agent_storage=AgentStorageConfig(), database=SimpleNamespace(backend="memory"))
    assert type(make_managed_subagent_store(plain)) is FileManagedSubagentStore


async def test_managed_thinking_reaches_the_runtime_config(monkeypatch):
    from deerflow.subagents import registry

    store = FileManagedSubagentStore()
    store.create(ManagedSubagentDefinition(name="build", description="Builds", system_prompt="Build it.", thinking_enabled=True))
    monkeypatch.setattr(registry, "get_managed_subagent_store", lambda *_a, **_k: store)
    registry._clear_managed_definitions_cache()
    config = registry._build_managed_subagent_config("build")
    assert config is not None and config.thinking_enabled is True


async def test_the_api_carries_thinking_and_where_the_definition_is_kept(monkeypatch):
    store = RepoStore()
    monkeypatch.setattr(router, "get_managed_subagent_store", lambda *_: store)
    created = await router.create_managed_subagent(
        _request(),
        router.ManagedSubagentCreateRequest(name="build", description="Builds", system_prompt="Build it.", thinking_enabled=True),
    )
    assert created.thinking_enabled is True
    assert created.stored_in["url"].endswith("/subagents/build.md")
    # An update that does not mention thinking keeps it.
    updated = await router.update_managed_subagent("build", _request(), router.ManagedSubagentUpdateRequest(description="Builds things"))
    assert updated.thinking_enabled is True and updated.description == "Builds things"
    listed = {s.name: s for s in router._catalog(True).subagents}
    assert listed["build"].stored_in is not None
    plain = router.SubagentResponse(name="x", description="d", source="builtin").model_dump()
    assert "stored_in" not in plain


@pytest.mark.parametrize("status", [409, 422, 502])
async def test_a_store_refusal_is_the_http_answer(monkeypatch, status):
    store = RepoStore()
    store.create(ManagedSubagentDefinition(name="build", description="Builds", system_prompt="Build it."))
    monkeypatch.setattr(router, "get_managed_subagent_store", lambda *_: store)
    RepoStore.refuse = AgentStoreError(f"refused {status}", status)
    with pytest.raises(HTTPException) as update:
        await router.update_managed_subagent("build", _request(), router.ManagedSubagentUpdateRequest(description="x"))
    assert update.value.status_code == status and update.value.detail == f"refused {status}"
    with pytest.raises(HTTPException) as delete:
        await router.delete_managed_subagent("build", _request())
    assert delete.value.status_code == status

"""[argus patch #109] A deployment-supplied agent store (agent_storage.store_class)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml
from fastapi.testclient import TestClient

from deerflow.config.agent_storage_config import AgentStorageConfig
from deerflow.config.agents_api_config import AgentsApiConfig, get_agents_api_config, set_agents_api_config
from deerflow.config.paths import Paths
from deerflow.persistence import agents as agents_pkg
from deerflow.persistence.agents import AgentStoreError, make_agent_store
from deerflow.persistence.agents.file import FileAgentStore


class RepoStore(FileAgentStore):
    """Like Argus's git-backed store: owns the shared layout, keeps an authored
    soul apart from the one the model sees, and can refuse a write."""

    refuse: AgentStoreError | None = None
    writes: list = []

    def is_legacy_only(self, name, *, user_id=None):
        return False

    def get_editable_soul(self, name, *, user_id=None):
        return "<!-- note -->\nAuthored soul."

    def source_info(self, name, *, user_id=None):
        return {"kind": "github", "url": f"https://github.com/acme/agents/tree/main/agents/{name}"}

    def update(self, name, config, soul, *, user_id=None):
        if RepoStore.refuse:
            raise RepoStore.refuse
        RepoStore.writes.append((name, config, soul))


def _app_config(**storage):
    return SimpleNamespace(
        agent_storage=AgentStorageConfig(**storage),
        database=SimpleNamespace(backend="memory"),
        get_model_config=lambda name: SimpleNamespace(name=name) if name == "local" else None,
    )


def test_store_class_selects_one_instance_per_path():
    agents_pkg._custom_stores.clear()
    config = _app_config(store_class="test_agent_store_class:RepoStore")
    first = make_agent_store(config)
    assert isinstance(first, RepoStore) and make_agent_store(config) is first
    assert isinstance(make_agent_store(_app_config()), FileAgentStore)
    assert not isinstance(make_agent_store(_app_config()), RepoStore)


def test_store_class_must_be_an_agent_store():
    agents_pkg._custom_stores.clear()
    with pytest.raises(ValueError):
        make_agent_store(_app_config(store_class="pathlib:Path"))


def test_stock_stores_keep_upstream_defaults(tmp_path):
    store = FileAgentStore()
    with patch("deerflow.config.agents_config.get_paths", return_value=Paths(base_dir=tmp_path)):
        legacy = tmp_path / "agents" / "shared"
        legacy.mkdir(parents=True)
        (legacy / "config.yaml").write_text("name: shared\n")
        (legacy / "SOUL.md").write_text("Shared soul.")
        assert store.is_legacy_only("shared", user_id="u1") is True
        assert store.get_editable_soul("shared", user_id="u1") == "Shared soul."
        assert store.source_info("shared", user_id="u1") is None


@pytest.fixture()
def client(tmp_path):
    from _router_auth_helpers import make_authed_test_app

    import app.gateway.routers.agents as router

    paths = Paths(base_dir=tmp_path)
    shared = tmp_path / "agents" / "atlas"
    shared.mkdir(parents=True)
    (shared / "config.yaml").write_text(yaml.safe_dump({"name": "atlas", "model": "local", "description": "Main"}))
    (shared / "SOUL.md").write_text("Composed soul the model sees.")
    store = RepoStore()
    RepoStore.refuse = None
    RepoStore.writes = []
    previous = AgentsApiConfig(**get_agents_api_config().model_dump())
    with (
        patch("deerflow.config.agents_config.get_paths", return_value=paths),
        patch.object(router, "get_paths", return_value=paths),
        patch.object(router, "get_app_config", lambda: _app_config()),
        patch.object(router, "get_agent_store", return_value=store),
        patch("deerflow.persistence.agents.get_agent_store", return_value=store),
    ):
        set_agents_api_config(AgentsApiConfig(enabled=True))
        try:
            app = make_authed_test_app()
            app.include_router(router.router)
            with TestClient(app) as c:
                yield c
        finally:
            set_agents_api_config(previous)


def test_get_shows_the_authored_soul_and_where_it_lives(client):
    body = client.get("/api/agents/atlas").json()
    assert body["soul"] == "<!-- note -->\nAuthored soul."
    assert body["source"] == {"kind": "github", "url": "https://github.com/acme/agents/tree/main/agents/atlas"}
    exported = client.get("/api/agents/atlas/export").json()
    assert exported["agent"]["soul"] == "<!-- note -->\nAuthored soul."


def test_a_shared_layout_agent_is_editable_when_the_store_owns_it(client):
    resp = client.put("/api/agents/atlas", json={"soul": "New soul."})
    assert resp.status_code == 200
    assert RepoStore.writes == [("atlas", None, "New soul.")]


@pytest.mark.parametrize("status", [409, 422, 502])
def test_a_store_refusal_is_the_http_answer(client, status):
    RepoStore.refuse = AgentStoreError(f"refused {status}", status)
    resp = client.put("/api/agents/atlas", json={"description": "x"})
    assert resp.status_code == status and resp.json()["detail"] == f"refused {status}"


def test_update_agent_tool_reports_a_store_refusal(tmp_path):
    from deerflow.tools.builtins import update_agent_tool as tool_mod

    paths = Paths(base_dir=tmp_path)
    shared = tmp_path / "agents" / "atlas"
    shared.mkdir(parents=True)
    (shared / "config.yaml").write_text("name: atlas\nmodel: local\n")
    store = RepoStore()
    RepoStore.refuse = AgentStoreError("Changed on GitHub since this page loaded. Reload and try again.", 409)
    runtime = SimpleNamespace(tool_call_id="t1", context={"agent_name": "atlas", "user_id": "u1"}, config={})
    with (
        patch("deerflow.config.agents_config.get_paths", return_value=paths),
        patch.object(tool_mod, "get_paths", return_value=paths),
        patch.object(tool_mod, "get_agent_store", return_value=store),
        patch("deerflow.persistence.agents.get_agent_store", return_value=store),
        patch.object(tool_mod, "get_app_config", lambda: _app_config()),
        patch.object(tool_mod, "resolve_runtime_user_id", lambda _rt: "u1"),
    ):
        result = tool_mod.update_agent.func(runtime=runtime, soul="Be brief.")
    message = result.update["messages"][0]
    assert message.status == "error" and "Changed on GitHub" in message.content
    RepoStore.refuse = None

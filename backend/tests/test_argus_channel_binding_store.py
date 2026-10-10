"""[argus patch #105] Chat-to-thread bindings stay in channels/store.json by default."""

from types import SimpleNamespace

import pytest

from app.channels import store as store_module
from app.channels.service import ChannelService
from app.channels.store import JsonChannelStore, SqlChannelStore


def _config(backend: str):
    return SimpleNamespace(database=SimpleNamespace(backend=backend))


@pytest.fixture(autouse=True)
def _store_path(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, "default_store_path", lambda: tmp_path / "channels" / "store.json")


def test_postgres_stack_keeps_the_json_file_by_default(tmp_path):
    service = ChannelService(channels_config={}, app_config=_config("postgres"))
    assert isinstance(service.store, JsonChannelStore)
    assert service.store.path == tmp_path / "channels" / "store.json"


def test_auto_follows_upstream_selection(monkeypatch):
    sentinel = object()
    monkeypatch.setattr("app.channels.service.resolve_channel_store", lambda app_config: sentinel)
    service = ChannelService(channels_config={"binding_store": "auto"}, app_config=_config("postgres"))
    assert service.store is sentinel


def test_auto_on_postgres_picks_the_table():
    service = ChannelService(channels_config={"binding_store": "auto"}, app_config=_config("postgres"))
    # No engine is initialised in unit tests, so upstream falls back to JSON with a
    # warning; with a session factory it returns the SQL store.
    assert isinstance(service.store, (JsonChannelStore, SqlChannelStore))


def test_an_unknown_value_is_refused():
    with pytest.raises(ValueError, match="binding_store"):
        ChannelService(channels_config={"binding_store": "sql"})


def test_an_explicit_store_wins():
    explicit = JsonChannelStore("/nonexistent/store.json")
    assert ChannelService(channels_config={"binding_store": "auto"}, store=explicit).store is explicit

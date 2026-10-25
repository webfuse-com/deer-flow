"""[argus patch #150] A chat bound to a deleted thread heals once and says so.

``_ensure_thread_registered`` verifies a binding once per process
(``_registered_threads``) and then trusts it. When the thread was deleted later
(atlas-ion deleted his Telegram DM thread from the web sidebar, 2026-10-16),
every message's run got "Thread ... not found" and the person was told "An
error occurred while processing your request. Please try again." 7 times over
two days. Now the run path evicts the cached check, heals the binding the same
way the first check does, retries once and tells the person the conversation
starts over. The thread DELETE route evicts the cache too.
"""

import asyncio
import copy
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from langgraph_sdk.errors import NotFoundError

from app.channels.manager import MISSING_THREAD_NOTICE, ChannelManager, _is_missing_thread_error, _select_carried_agent_name
from app.channels.message_bus import InboundMessage, MessageBus, OutboundMessage
from app.channels.store import JsonChannelStore

GENERIC_ERROR = "An error occurred while processing your request. Please try again."
ATLAS_SESSIONS = {"telegram": {"context": {"agent_name": "atlas"}}, "slack": {"context": {"agent_name": "atlas"}}}


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _not_found(text: str) -> NotFoundError:
    return NotFoundError(text, response=httpx.Response(404, request=httpx.Request("POST", "http://gateway/api/threads/x/runs/stream")), body=None)


def _events():
    return [
        SimpleNamespace(event="values", data={"messages": [{"type": "human", "content": "hi"}, {"type": "ai", "content": "Hello again"}], "artifacts": []}),
    ]


class _Gateway:
    """Threads registry + run endpoints, enough for the manager's chat path."""

    def __init__(self, *, deleted=(), always_missing=(), other_404: bool = False):
        self.deleted = set(deleted)
        self.always_missing = set(always_missing)
        self.other_404 = other_404
        self.creates: list[dict] = []
        self.stream_calls: list[str] = []
        self.wait_calls: list[str] = []
        self.run_kwargs: list[dict] = []
        # Per run request: the Idempotency-Key header and the human message id.
        self.run_keys: list[tuple[str | None, str | None]] = []
        self.client = MagicMock()
        self.client.threads.get = self._get
        self.client.threads.create = self._create
        self.client.threads.update = AsyncMock(return_value={})
        self.client.runs.stream = self._stream
        self.client.runs.wait = self._wait

    def _missing(self, thread_id: str) -> bool:
        return thread_id in self.deleted or thread_id in self.always_missing

    async def _get(self, thread_id, **kwargs):
        if self._missing(thread_id):
            raise _not_found(f"Thread {thread_id} not found")
        return {"thread_id": thread_id, "metadata": {}}

    async def _create(self, **kwargs):
        self.creates.append(kwargs)
        self.deleted.discard(kwargs.get("thread_id", ""))
        return {"thread_id": kwargs.get("thread_id") or "thread-new"}

    def _record_keys(self, kwargs) -> None:
        headers = kwargs.get("headers") or {}
        messages = (kwargs.get("input") or {}).get("messages") or [{}]
        self.run_keys.append((headers.get("Idempotency-Key"), messages[0].get("id")))

    def _stream(self, thread_id, assistant_id, **kwargs):
        self.stream_calls.append(thread_id)
        self.run_kwargs.append(copy.deepcopy({key: kwargs.get(key) for key in ("config", "context")}))
        self._record_keys(kwargs)

        async def gen():
            if self.other_404:
                raise _not_found("Run r1 not found")
            if self._missing(thread_id):
                raise _not_found(f"Thread {thread_id} not found")
            for event in _events():
                yield event

        return gen()

    async def _wait(self, thread_id, assistant_id, **kwargs):
        self.wait_calls.append(thread_id)
        self.run_kwargs.append(copy.deepcopy({key: kwargs.get(key) for key in ("config", "context")}))
        self._record_keys(kwargs)
        if self._missing(thread_id):
            raise _not_found(f"Thread {thread_id} not found")
        return {"messages": [{"type": "human", "content": "hi"}, {"type": "ai", "content": "Hello again"}]}


async def _chat(gateway: _Gateway, msg: InboundMessage, *, cached: bool = True, bound: str = "thread-old", agent_pin: str | None = None) -> tuple[ChannelManager, list[OutboundMessage]]:
    manager = ChannelManager(bus=MessageBus(), store=JsonChannelStore(path=Path(tempfile.mkdtemp()) / "store.json"), channel_sessions=ATLAS_SESSIONS)
    await manager.store.set_thread_id(msg.channel_name, msg.chat_id, bound, topic_id=msg.topic_id, user_id=msg.user_id)
    manager._client = gateway.client
    if cached:
        # This process already verified the binding and loaded its pins.
        manager._registered_threads.add(bound)
        manager._thread_agent_names[bound] = agent_pin
    manager._thread_model_names[bound] = None
    outbound: list[OutboundMessage] = []

    async def capture(message: OutboundMessage) -> None:
        outbound.append(message)

    manager.bus.subscribe_outbound(capture)
    await manager._handle_chat(msg, bound_identity_checked=True)
    return manager, outbound


def _telegram(**kwargs) -> InboundMessage:
    return InboundMessage(channel_name="telegram", chat_id="111", user_id="111", text="When is my meeting?", metadata={"message_id": "7"}, **kwargs)


def test_missing_thread_error_matches_only_that_thread():
    assert _is_missing_thread_error(_not_found("Thread t1 not found"), "t1")
    assert not _is_missing_thread_error(_not_found("Thread t2 not found"), "t1")
    assert not _is_missing_thread_error(_not_found("Run r1 not found"), "t1")
    assert not _is_missing_thread_error(RuntimeError("Thread t1 not found"), "t1")


def test_streaming_run_on_a_deleted_thread_heals_retries_and_says_so():
    gateway = _Gateway(deleted={"thread-old"})
    manager, outbound = _run(_chat(gateway, _telegram()))
    assert gateway.stream_calls == ["thread-old", "thread-old"]
    assert [c["thread_id"] for c in gateway.creates] == ["thread-old"]
    assert gateway.creates[0]["metadata"]["agent_name"] == "atlas"
    texts = [(m.is_final, m.text) for m in outbound if m.text]
    assert (False, MISSING_THREAD_NOTICE) in texts
    assert (True, "Hello again") in texts
    assert all(GENERIC_ERROR not in text for _, text in texts)
    assert "thread-old" in manager._registered_threads


def test_the_heal_retries_only_once():
    gateway = _Gateway(always_missing={"thread-old"})
    _manager, outbound = _run(_chat(gateway, _telegram()))
    assert gateway.stream_calls == ["thread-old", "thread-old"]
    finals = [m.text for m in outbound if m.is_final]
    assert finals == [GENERIC_ERROR]


def test_a_refused_reregistration_retries_on_the_fresh_thread():
    gateway = _Gateway(deleted={"thread-old"})

    async def refuse_same_id(**kwargs):
        gateway.creates.append(kwargs)
        if kwargs.get("thread_id"):
            raise RuntimeError("refused")
        return {"thread_id": "thread-new"}

    gateway.client.threads.create = refuse_same_id
    manager, outbound = _run(_chat(gateway, _telegram()))
    assert gateway.stream_calls == ["thread-old", "thread-new"]
    assert [m.thread_id for m in outbound if m.is_final] == ["thread-new"]
    assert _run(manager.store.get_thread_id("telegram", "111")) == "thread-new"


def test_another_not_found_is_not_healed():
    gateway = _Gateway(other_404=True)
    _manager, outbound = _run(_chat(gateway, _telegram()))
    assert gateway.stream_calls == ["thread-old"]
    assert gateway.creates == []
    assert [m.text for m in outbound if m.is_final] == [GENERIC_ERROR]


def test_an_unattended_fire_heals_without_a_notice():
    gateway = _Gateway(deleted={"thread-old"})
    msg = _telegram(topic_id="sched:daily-review", agent_name="atlas", unattended=True)
    _manager, outbound = _run(_chat(gateway, msg))
    assert gateway.stream_calls == ["thread-old", "thread-old"]
    assert all(m.text != MISSING_THREAD_NOTICE for m in outbound)


def test_wait_path_heals_and_retries_once():
    gateway = _Gateway(deleted={"thread-old"})
    msg = InboundMessage(channel_name="slack", chat_id="C1", user_id="U1", text="hi", metadata={"message_id": "1"})
    _manager, outbound = _run(_chat(gateway, msg))
    assert gateway.wait_calls == ["thread-old", "thread-old"]
    texts = [m.text for m in outbound]
    assert MISSING_THREAD_NOTICE in texts and "Hello again" in texts


def test_loading_the_thread_agent_heals_a_thread_deleted_after_its_check():
    """Agent pin not cached yet: the 404 surfaces in _load_thread_agent, before the run."""
    gateway = _Gateway(deleted={"thread-old"})
    manager = None

    async def go():
        nonlocal manager
        manager = ChannelManager(bus=MessageBus(), store=JsonChannelStore(path=Path(tempfile.mkdtemp()) / "store.json"), channel_sessions=ATLAS_SESSIONS)
        await manager.store.set_thread_id("telegram", "111", "thread-old", user_id="111")
        manager._client = gateway.client
        manager._registered_threads.add("thread-old")
        manager._thread_model_names["thread-old"] = None
        outbound: list[OutboundMessage] = []

        async def capture(message: OutboundMessage) -> None:
            outbound.append(message)

        manager.bus.subscribe_outbound(capture)
        await manager._handle_chat(_telegram(), bound_identity_checked=True)
        return outbound

    outbound = _run(go())
    assert gateway.stream_calls == ["thread-old"]
    assert [c["thread_id"] for c in gateway.creates] == ["thread-old"]
    assert MISSING_THREAD_NOTICE in [m.text for m in outbound]
    assert [m.text for m in outbound if m.is_final] == ["Hello again"]


def test_another_owners_thread_is_not_healed_and_no_notice_is_sent():
    """The gateway answers a foreign thread with the same 404 as a deleted one; re-creating the id tells them apart."""
    gateway = _Gateway(always_missing={"thread-old"})

    async def foreign(**kwargs):
        gateway.creates.append(kwargs)
        if kwargs.get("thread_id") == "thread-old":
            raise _not_found("Thread not found")
        return {"thread_id": "thread-new"}

    gateway.client.threads.create = foreign
    manager, outbound = _run(_chat(gateway, _telegram()))
    assert gateway.stream_calls == ["thread-old"]
    assert all(m.text != MISSING_THREAD_NOTICE for m in outbound)
    assert [m.text for m in outbound if m.is_final] == [GENERIC_ERROR]
    assert _run(manager.store.get_thread_id("telegram", "111")) == "thread-old"
    assert "thread-old" in manager._registered_threads


def test_another_owners_thread_on_the_wait_path_is_not_healed():
    gateway = _Gateway(always_missing={"thread-old"})

    async def foreign(**kwargs):
        gateway.creates.append(kwargs)
        raise _not_found("Thread not found")

    gateway.client.threads.create = foreign
    msg = InboundMessage(channel_name="slack", chat_id="C1", user_id="U1", text="hi", metadata={"message_id": "1"})
    outbound: list[OutboundMessage] = []

    async def go():
        manager = ChannelManager(bus=MessageBus(), store=JsonChannelStore(path=Path(tempfile.mkdtemp()) / "store.json"), channel_sessions=ATLAS_SESSIONS)
        await manager.store.set_thread_id("slack", "C1", "thread-old", user_id="U1")
        manager._client = gateway.client
        manager._registered_threads.add("thread-old")
        manager._thread_agent_names["thread-old"] = None
        manager._thread_model_names["thread-old"] = None

        async def capture(message: OutboundMessage) -> None:
            outbound.append(message)

        manager.bus.subscribe_outbound(capture)
        with pytest.raises(NotFoundError):
            # The original error goes to the dispatcher's generic handler, as before #150.
            await manager._handle_chat(msg, bound_identity_checked=True)

    _run(go())
    assert gateway.wait_calls == ["thread-old"]
    assert all(m.text != MISSING_THREAD_NOTICE for m in outbound)


def test_the_retry_does_not_carry_the_deleted_threads_agent_pin():
    """A cached /agent use pin belonged to the deleted thread; the retry resolves the run again."""
    gateway = _Gateway(deleted={"thread-old"})
    _manager, outbound = _run(_chat(gateway, _telegram(), agent_pin="coder"))
    assert gateway.stream_calls == ["thread-old", "thread-old"]
    first, retry = gateway.run_kwargs
    assert _select_carried_agent_name(first["config"], first["context"]) == "coder"
    assert _select_carried_agent_name(retry["config"], retry["context"]) == "atlas"
    assert retry["context"]["thread_id"] == "thread-old" and retry["config"]["configurable"]["thread_id"] == "thread-old"
    assert [m.text for m in outbound if m.is_final] == ["Hello again"]


def test_the_wait_retry_does_not_carry_the_deleted_threads_agent_pin():
    gateway = _Gateway(deleted={"thread-old"})
    msg = InboundMessage(channel_name="slack", chat_id="C1", user_id="U1", text="hi", metadata={"message_id": "1"})
    _run(_chat(gateway, msg, agent_pin="coder"))
    first, retry = gateway.run_kwargs
    assert _select_carried_agent_name(first["config"], first["context"]) == "coder"
    assert _select_carried_agent_name(retry["config"], retry["context"]) == "atlas"


def test_forget_thread_drops_every_cached_fact():
    manager = ChannelManager(bus=MessageBus(), store=JsonChannelStore(path=Path(tempfile.mkdtemp()) / "store.json"))
    manager._registered_threads.add("t1")
    manager._channel_metadata_synced.add("t1")
    manager._thread_agent_names["t1"] = "coder"
    manager._thread_model_names["t1"] = "local-qwen"
    generation = manager._model_cache_generation
    manager.forget_thread("t1")
    assert "t1" not in manager._registered_threads and "t1" not in manager._channel_metadata_synced
    assert "t1" not in manager._thread_agent_names and "t1" not in manager._thread_model_names
    assert manager._model_cache_generation == generation + 1


def test_thread_delete_route_evicts_the_channel_caches(tmp_path):
    from _router_auth_helpers import make_authed_test_app
    from fastapi.testclient import TestClient

    from app.gateway.routers import threads
    from deerflow.config.paths import Paths

    class _RunManager:
        async def list_by_thread(self, _thread_id, *, user_id=None, limit=100):
            return []

        @asynccontextmanager
        async def reserve_thread_operation(self, _thread_id, **_kwargs):
            yield

    app = make_authed_test_app()
    app.state.run_manager = _RunManager()
    app.include_router(threads.router)
    channel_manager = MagicMock()
    with (
        patch("app.gateway.routers.threads.get_paths", return_value=Paths(tmp_path)),
        patch("app.channels.service.get_channel_service", return_value=SimpleNamespace(manager=channel_manager)),
    ):
        with TestClient(app) as client:
            response = client.delete("/api/threads/thread-deleted")
    assert response.status_code == 200
    channel_manager.forget_thread.assert_called_once_with("thread-deleted")


def test_thread_delete_route_without_channels_still_succeeds(tmp_path):
    from _router_auth_helpers import make_authed_test_app
    from fastapi.testclient import TestClient

    from app.gateway.routers import threads
    from deerflow.config.paths import Paths

    class _RunManager:
        async def list_by_thread(self, _thread_id, *, user_id=None, limit=100):
            return []

        @asynccontextmanager
        async def reserve_thread_operation(self, _thread_id, **_kwargs):
            yield

    app = make_authed_test_app()
    app.state.run_manager = _RunManager()
    app.include_router(threads.router)
    with (
        patch("app.gateway.routers.threads.get_paths", return_value=Paths(tmp_path)),
        patch("app.channels.service.get_channel_service", return_value=None),
    ):
        with TestClient(app) as client:
            assert client.delete("/api/threads/thread-x").status_code == 200


# -- sync 2026-10-25: the heal under upstream's idempotent run admission (#7104) --


@pytest.mark.parametrize("path", ["stream", "wait"])
@pytest.mark.parametrize("refused", [False, True], ids=["same-id", "fresh-thread"])
def test_the_retry_keeps_the_messages_idempotency_key_and_message_id(path, refused):
    """The healed retry is the same request as far as admission is concerned:
    the same Idempotency-Key and the same keyed human message id, also when it
    lands on a fresh thread. The Gateway refused the first attempt before it
    admitted anything (see the start_run test below), so the retry is
    admitted as this message's run."""
    gateway = _Gateway(deleted={"thread-old"})
    if refused:

        async def refuse_same_id(**kwargs):
            gateway.creates.append(kwargs)
            if kwargs.get("thread_id"):
                raise RuntimeError("refused")
            return {"thread_id": "thread-new"}

        gateway.client.threads.create = refuse_same_id
    if path == "stream":
        msg = _telegram()
    else:
        msg = InboundMessage(channel_name="slack", chat_id="C1", user_id="U1", text="hi", metadata={"message_id": "1", "team_id": "T1"})
    _manager, outbound = _run(_chat(gateway, msg))

    key = ChannelManager._run_idempotency_key(msg)
    message_id = ChannelManager._human_message_id(msg)
    assert key and message_id
    assert gateway.run_keys == [(key, message_id), (key, message_id)]
    calls = gateway.stream_calls if path == "stream" else gateway.wait_calls
    assert calls == ["thread-old", "thread-new" if refused else "thread-old"]
    assert "Hello again" in [m.text for m in outbound]


def test_a_healed_thread_tells_the_adapter_the_old_one_was_replaced(monkeypatch):
    """Upstream's ``on_thread_replaced`` (#7357) retires per-thread adapter state
    (pending interactive cards) when a conversation moves on, as ``/new`` does."""
    replaced: list[str] = []
    channel = SimpleNamespace(on_thread_replaced=replaced.append)
    service = SimpleNamespace(get_channel=lambda name: channel if name == "telegram" else None)
    monkeypatch.setattr("app.channels.service.get_channel_service", lambda: service)
    monkeypatch.setattr(ChannelManager, "_channel_supports_streaming", staticmethod(lambda name: True))

    gateway = _Gateway(deleted={"thread-old"})
    _run(_chat(gateway, _telegram()))
    assert replaced == ["thread-old"]

    replaced.clear()
    gateway = _Gateway(deleted={"thread-old"})

    async def refuse_same_id(**kwargs):
        gateway.creates.append(kwargs)
        if kwargs.get("thread_id"):
            raise RuntimeError("refused")
        return {"thread_id": "thread-new"}

    gateway.client.threads.create = refuse_same_id
    _run(_chat(gateway, _telegram()))
    assert replaced == ["thread-old"]


@pytest.mark.anyio
async def test_the_gateway_refuses_a_missing_thread_before_admission_so_the_keyed_retry_is_admitted():
    """The Gateway half of the contract the heal relies on.

    ``start_run`` answers "Thread X not found" before ``create_or_reject`` sees
    the Idempotency-Key, so nothing is recorded for the refused attempt. The
    retry with the same key and input, on the re-registered thread or on a
    fresh one, is admitted as a new run, not attached to a run of the deleted
    thread and not refused as "Idempotency-Key already used".
    """
    from fastapi import HTTPException
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.store.memory import InMemoryStore

    from app.channels.manager import _human_input_message
    from app.gateway.run_models import RunCreateRequest
    from app.gateway.services import start_run
    from deerflow.config.app_config import AppConfig, reset_app_config, set_app_config
    from deerflow.persistence.thread_meta.memory import MemoryThreadMetaStore
    from deerflow.runtime import RunManager
    from deerflow.runtime.events.store.memory import MemoryRunEventStore
    from deerflow.runtime.runs.store.memory import MemoryRunStore

    msg = _telegram()
    key = f"internal-run:{ChannelManager._run_idempotency_key(msg)}"

    def body() -> RunCreateRequest:
        return RunCreateRequest(input={"messages": [_human_input_message(msg.text, message_id=ChannelManager._human_message_id(msg))]}, on_disconnect="continue")

    store = InMemoryStore()
    thread_store = MemoryThreadMetaStore(store)
    run_store = MemoryRunStore()
    run_manager = RunManager(store=run_store, worker_id="worker-a")
    request = SimpleNamespace(
        headers={},
        state=SimpleNamespace(auth_source=None, user=None),
        app=SimpleNamespace(
            state=SimpleNamespace(
                stream_bridge=SimpleNamespace(),
                run_manager=run_manager,
                checkpointer=InMemorySaver(),
                store=store,
                run_event_store=MemoryRunEventStore(),
                run_events_config=None,
                thread_store=thread_store,
            )
        ),
    )

    async def fake_run_agent(*args, **kwargs):
        del args, kwargs
        await asyncio.Event().wait()

    set_app_config(AppConfig.model_validate({"sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"}}))
    admitted = []
    try:
        with (
            patch("app.gateway.services.resolve_agent_factory", return_value=object()),
            patch("app.gateway.services.run_agent", side_effect=fake_run_agent),
        ):
            with pytest.raises(HTTPException) as excinfo:
                await start_run(body(), "thread-old", request, idempotency_key=key, require_existing_thread=True)
            assert excinfo.value.status_code == 404
            assert "Thread thread-old not found" in str(excinfo.value.detail)
            assert await run_store.list_by_thread("thread-old") == []

            # The heal: the same id registered again, or a fresh thread bound.
            for thread_id in ("thread-old", "thread-new"):
                await thread_store.create(thread_id)
            record = await start_run(body(), "thread-old", request, idempotency_key=key, require_existing_thread=True)
            admitted.append(record)
            assert record.idempotency_reused is False
            assert [r["run_id"] for r in await run_store.list_by_thread("thread-old")] == [record.run_id]

        # The fresh-thread variant on its own Gateway (one key per owner).
        run_store_b = MemoryRunStore()
        request.app.state.run_manager = RunManager(store=run_store_b, worker_id="worker-b")
        with (
            patch("app.gateway.services.resolve_agent_factory", return_value=object()),
            patch("app.gateway.services.run_agent", side_effect=fake_run_agent),
        ):
            with pytest.raises(HTTPException):
                await start_run(body(), "thread-gone", request, idempotency_key=key, require_existing_thread=True)
            record_b = await start_run(body(), "thread-new", request, idempotency_key=key, require_existing_thread=True)
            admitted.append(record_b)
            assert record_b.idempotency_reused is False
    finally:
        for record in admitted:
            if record.task is not None:
                record.task.cancel()
                await asyncio.gather(record.task, return_exceptions=True)
        reset_app_config()

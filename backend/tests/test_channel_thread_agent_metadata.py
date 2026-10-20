"""[argus patch #149] A thread a channel creates records the agent its runs use.

``_get_or_create_thread`` created IM threads with channel metadata only, so
``pathOfThread`` sent a scheduled or Telegram thread opened in the browser to
``/workspace/chats/<id>``, which runs the default agent: no SOUL, no Atlas base
prompt (atlas-quinten thread 311ba158, 16 web runs as ``__default__``,
2026-10-16; 435 of 435 channel threads created that week had no agent). The
agent is recorded as ``agent_name`` (the key the web route reads) without
pinning it as ``channel_agent_name``, so channel config changes still apply.
"""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import httpx
from langgraph_sdk.errors import NotFoundError

from app.channels.manager import CHANNEL_AGENT_METADATA_KEY, THREAD_AGENT_METADATA_KEY, ChannelManager
from app.channels.message_bus import InboundMessage, MessageBus
from app.channels.store import JsonChannelStore

ATLAS_SESSIONS = {"telegram": {"context": {"agent_name": "atlas", "user_email": "owner@example.com"}}}


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _manager(**kwargs) -> ChannelManager:
    return ChannelManager(bus=MessageBus(), store=JsonChannelStore(path=Path(tempfile.mkdtemp()) / "store.json"), **kwargs)


def _client(created: list[dict], *, missing: set[str] | None = None) -> MagicMock:
    missing = missing if missing is not None else set()

    async def create(**kwargs):
        created.append(kwargs)
        missing.discard(kwargs.get("thread_id", ""))
        return {"thread_id": kwargs.get("thread_id") or f"thread-{len(created)}"}

    async def get(thread_id, **kwargs):
        if thread_id in missing:
            raise NotFoundError(f"Thread {thread_id} not found", response=httpx.Response(404, request=httpx.Request("GET", "http://gateway/api/threads")), body=None)
        return {"thread_id": thread_id, "metadata": {}}

    client = MagicMock()
    client.threads.create = create
    client.threads.get = get
    return client


def test_telegram_thread_records_the_session_agent_without_pinning_it():
    async def go():
        manager = _manager(channel_sessions=ATLAS_SESSIONS)
        created: list[dict] = []
        msg = InboundMessage(channel_name="telegram", chat_id="111", user_id="111", text="hi")
        thread_id, was_created = await manager._get_or_create_thread(_client(created), msg)
        assert was_created is True
        metadata = created[0]["metadata"]
        assert metadata[THREAD_AGENT_METADATA_KEY] == "atlas"
        assert CHANNEL_AGENT_METADATA_KEY not in metadata
        assert metadata["channel_source"]["provider"] == "telegram"
        # Not a pin: the next run still resolves the agent from the session layers.
        assert manager._thread_agent_names[thread_id] is None

    _run(go())


def test_scheduled_fire_records_its_runner():
    async def go():
        manager = _manager()
        created: list[dict] = []
        msg = InboundMessage(channel_name="telegram", chat_id="111", user_id="111", text="Run outreach-sync", topic_id="sched:outreach-sync", agent_name="atlas", unattended=True)
        await manager._get_or_create_thread(_client(created), msg)
        assert created[0]["metadata"][THREAD_AGENT_METADATA_KEY] == "atlas"

    _run(go())


def test_assistant_id_session_routes_to_the_normalized_custom_agent():
    async def go():
        manager = _manager(channel_sessions={"slack": {"assistant_id": "Pythia_Internal"}})
        created: list[dict] = []
        await manager._get_or_create_thread(_client(created), InboundMessage(channel_name="slack", chat_id="C1", user_id="U1", text="hi"))
        assert created[0]["metadata"][THREAD_AGENT_METADATA_KEY] == "pythia-internal"

    _run(go())


def test_lead_agent_threads_stay_as_upstream():
    async def go():
        manager = _manager()
        created: list[dict] = []
        await manager._get_or_create_thread(_client(created), InboundMessage(channel_name="telegram", chat_id="111", user_id="111", text="hi"))
        assert THREAD_AGENT_METADATA_KEY not in created[0]["metadata"]

    _run(go())


def test_explicit_agent_selection_keeps_its_pin():
    async def go():
        manager = _manager(channel_sessions=ATLAS_SESSIONS)
        created: list[dict] = []
        msg = InboundMessage(channel_name="telegram", chat_id="111", user_id="111", text="/agent use coder")
        await manager._create_thread(_client(created), msg, agent_name="coder")
        metadata = created[0]["metadata"]
        assert metadata[CHANNEL_AGENT_METADATA_KEY] == "coder"
        assert metadata[THREAD_AGENT_METADATA_KEY] == "coder"

    _run(go())


def test_reregistered_thread_records_the_agent_too():
    async def go():
        manager = _manager(channel_sessions=ATLAS_SESSIONS)
        await manager.store.set_thread_id("telegram", "111", "thread-old", user_id="111")
        created: list[dict] = []
        msg = InboundMessage(channel_name="telegram", chat_id="111", user_id="111", text="hi")
        assert await manager._get_or_create_thread(_client(created, missing={"thread-old"}), msg) == ("thread-old", False)
        assert created[0]["thread_id"] == "thread-old"
        assert created[0]["metadata"][THREAD_AGENT_METADATA_KEY] == "atlas"

    _run(go())


def test_invalid_session_agent_never_blocks_thread_creation():
    async def go():
        manager = _manager(channel_sessions={"slack": {"assistant_id": "not a valid name!"}})
        created: list[dict] = []
        await manager._get_or_create_thread(_client(created), InboundMessage(channel_name="slack", chat_id="C1", user_id="U1", text="hi"))
        assert THREAD_AGENT_METADATA_KEY not in created[0]["metadata"]

    _run(go())

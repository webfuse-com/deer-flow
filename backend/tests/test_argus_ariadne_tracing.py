"""[argus patch #121] Ariadne: masking, the per-chat off switch, attribution."""

from __future__ import annotations

import types

import pytest

from deerflow.tracing import argus


@pytest.fixture(autouse=True)
def _clean_switch():
    argus.reset_untraced_threads()
    yield
    argus.reset_untraced_threads()


# --- masking -------------------------------------------------------------------
def test_mask_strips_images_and_credentials():
    img = "data:image/png;base64," + "A" * 5000
    out = argus.mask(data={"content": f"look {img} and sk-ant-oat01-{'x' * 30} and ghp_{'q' * 30}"})
    assert "AAAA" not in out["content"] and "[image/png omitted" in out["content"]
    assert out["content"].count("[redacted]") == 2
    block = argus.mask(data=[{"role": "user", "content": [{"type": "image_url", "image_url": {"url": img}}]}])
    assert block[0]["content"][0] == {"type": "image", "note": "image omitted by Ariadne"}


def test_mask_caps_strings_and_keeps_system_prompt_and_latest_messages():
    assert len(argus.mask(data="w" * (argus.STRING_CAP + 50))) < argus.STRING_CAP + 100
    msgs = [{"role": "system", "content": "SYSTEM"}] + [{"role": "user", "content": f"m{i} " + "z" * 100_000} for i in range(30)]
    out = argus.mask(data=msgs)
    assert out[0]["content"] == "SYSTEM"
    assert "earlier messages omitted by Ariadne" in out[1]["content"]
    assert out[-1]["content"].startswith("m29 ")
    state = argus.mask(data={"messages": msgs, "title": "t"})
    assert state["title"] == "t" and len(state["messages"]) < len(msgs)


def test_mask_never_ships_unmasked_data_on_a_bug(monkeypatch):
    monkeypatch.setattr(argus, "_mask_value", lambda v: (_ for _ in ()).throw(RuntimeError("bug")))
    assert argus.mask(data={"secret": "x"}) == "[omitted: masking failed]"


# --- the per-chat switch ---------------------------------------------------------
def _span(session: str | None):
    return types.SimpleNamespace(attributes={"session.id": session} if session else {})


def test_switched_off_chat_exports_nothing(monkeypatch):
    import langfuse

    monkeypatch.setattr(langfuse, "is_default_export_span", lambda span: True)
    argus.set_thread_untraced("t-off", True)
    assert argus.should_export_span(_span("t-off")) is False
    assert argus.should_export_span(_span("t-on")) is True
    argus.set_thread_untraced("t-off", False)
    assert argus.should_export_span(_span("t-off")) is True


def test_a_bare_model_root_is_filtered_by_its_callback_metadata(monkeypatch):
    """A model called without a chain around it (the memory updater) carries
    the chat only as observation metadata, not as session.id."""
    import langfuse

    monkeypatch.setattr(langfuse, "is_default_export_span", lambda span: True)
    argus.set_thread_untraced("t-mem", True)
    span = types.SimpleNamespace(attributes={"langfuse.observation.metadata.langfuse_session_id": "t-mem"})
    assert argus.should_export_span(span) is False


def test_failed_preload_fails_closed_until_a_run_reads_the_switch(monkeypatch):
    import langfuse

    monkeypatch.setattr(langfuse, "is_default_export_span", lambda span: True)
    argus.mark_preload_failed()
    assert argus.should_export_span(_span("t-unknown")) is False
    argus.set_thread_untraced("t-unknown", False)  # a run read it: tracing on
    assert argus.should_export_span(_span("t-unknown")) is True


def test_is_tracing_off_reads_the_metadata_key():
    assert argus.is_tracing_off({"ariadne_tracing": "off"})
    assert argus.is_tracing_off({"ariadne_tracing": " OFF "})
    assert not argus.is_tracing_off({"ariadne_tracing": "on"})
    assert not argus.is_tracing_off({})
    assert not argus.is_tracing_off(None)


@pytest.mark.asyncio
async def test_preload_loads_switched_off_threads_from_the_database():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from deerflow.persistence.base import Base
    from deerflow.persistence.thread_meta.model import ThreadMetaRow

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[ThreadMetaRow.__table__])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all(
            [
                ThreadMetaRow(thread_id="a", metadata_json={"ariadne_tracing": "off"}),
                ThreadMetaRow(thread_id="b", metadata_json={"ariadne_tracing": "on"}),
                ThreadMetaRow(thread_id="c", metadata_json={}),
            ]
        )
        await session.commit()
    assert await argus.preload_untraced_threads(factory) == 1
    assert argus.is_thread_untraced("a") and not argus.is_thread_untraced("b")
    await engine.dispose()


# --- attribution -------------------------------------------------------------------
def test_display_user_prefers_the_signed_in_email_then_the_owner(monkeypatch):
    import deerflow.runtime.user_context as uc

    monkeypatch.setattr(uc, "get_current_user", lambda: types.SimpleNamespace(id="u1", email="Ann@Surfly.com"))
    assert argus.display_user("u1") == "ann@surfly.com"
    monkeypatch.setattr(uc, "get_current_user", lambda: types.SimpleNamespace(id="owner"))
    monkeypatch.setenv("PYTHIA_CALLER_EMAIL", "tony@surfly.com")
    assert argus.display_user("owner") == "tony@surfly.com"
    monkeypatch.delenv("PYTHIA_CALLER_EMAIL")
    monkeypatch.delenv("ATLAS_STACK_OWNER_EMAIL", raising=False)
    assert argus.display_user("owner") == "owner"


def test_stack_and_channel_tags(monkeypatch):
    monkeypatch.setenv("ARGUS_PROJECT", "atlas-tony")
    assert argus.stack_tag() == "stack:tony"
    assert argus.channel_tag({}) == "channel:web"
    assert argus.channel_tag({"deerflow_origin": {"kind": "im_channel", "provider": "telegram"}}) == "channel:telegram"
    assert argus.channel_tag({"deerflow_origin": {"kind": "schedule"}}) == "channel:schedule"


# --- Telegram /notrace ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_notrace_flips_the_switch_and_the_export_filter(monkeypatch):
    from app.channels import manager as manager_mod
    from app.channels.commands import KNOWN_CHANNEL_COMMANDS

    assert "/notrace" in KNOWN_CHANNEL_COMMANDS
    updates = []

    class FakeThreads:
        async def get(self, thread_id, **kw):
            return {"metadata": {}}

        async def update(self, thread_id, metadata, **kw):
            updates.append((thread_id, metadata))

    fake = types.SimpleNamespace(_get_client=lambda: types.SimpleNamespace(threads=FakeThreads()))

    async def lookup(msg):
        return "t-tg"

    fake._lookup_thread_id = lookup
    monkeypatch.setattr(manager_mod, "_owner_headers", lambda msg: None)
    reply = await manager_mod.ChannelManager._toggle_tracing(fake, object(), "")
    assert updates == [("t-tg", {"ariadne_tracing": "off"})] and "off" in reply
    assert argus.is_thread_untraced("t-tg")
    reply = await manager_mod.ChannelManager._toggle_tracing(fake, object(), "on")
    assert updates[-1] == ("t-tg", {"ariadne_tracing": "on"}) and not argus.is_thread_untraced("t-tg")


def test_tracing_switch_is_not_conversation_activity():
    from app.gateway.routers.threads import _is_organization_metadata_patch

    assert _is_organization_metadata_patch({"ariadne_tracing": "off"})
    assert not _is_organization_metadata_patch({"ariadne_tracing": "maybe"})


# --- failed tools are marked ERROR (DeerFlow classifies after Langfuse's callback) ---
def _tool_message(content, status="success", name="bash"):
    from langchain_core.messages import ToolMessage

    return ToolMessage(content=content, tool_call_id="call_1", name=name, status=status)


def test_a_failed_bash_call_gets_a_short_masked_status_message():
    secret = "ghp_" + "a" * 36
    msg = argus.tool_error_message(_tool_message(f"fatal: detected dubious ownership\n{secret}\nExit Code: 128"))
    assert msg is not None and msg.startswith("fatal: detected dubious ownership") and msg.endswith("Exit Code: 128")
    assert secret not in msg and "[redacted]" in msg
    long = argus.tool_error_message(_tool_message("x" * 5000 + "\nExit Code: 1"))
    assert len(long) <= argus.TOOL_STATUS_MESSAGE_CAP + 5 and long.endswith("Exit Code: 1")


def test_only_results_deerflow_treats_as_errors_get_a_status_message():
    assert argus.tool_error_message(_tool_message("ok\nExit Code: 0")) is None
    assert argus.tool_error_message(_tool_message("all good")) is None
    assert argus.tool_error_message(_tool_message("Error: file not found", name="read_file")) is not None
    assert argus.tool_error_message(_tool_message("blocked", status="error", name="write_file")) == "blocked"
    assert argus.tool_error_message("Exit Code: 1") is None  # not a ToolMessage
    original = _tool_message("boom\nExit Code: 2")
    argus.tool_error_message(original)
    assert original.status == "success" and "deerflow_tool_meta" not in (original.additional_kwargs or {})


def test_the_langfuse_handler_exports_a_failed_tool_as_an_error_span():
    import uuid

    from langfuse import Langfuse
    from langfuse.langchain import CallbackHandler
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    public_key = f"pk-lf-test-{uuid.uuid4().hex[:8]}"
    client = Langfuse(public_key=public_key, secret_key="sk-lf-test", host="http://127.0.0.1:9", span_exporter=exporter, mask=argus.mask)
    handler = argus.callback_handler(CallbackHandler, public_key=public_key)
    for content in ("fatal: not a git repository\nExit Code: 128", "fine\nExit Code: 0"):
        run_id = uuid.uuid4()
        handler.on_tool_start({"name": "bash"}, "{}", run_id=run_id, inputs={"command": "git log"})
        handler.on_tool_end(_tool_message(content), run_id=run_id)
    client.flush()
    spans = {s.attributes.get("langfuse.observation.output", ""): s for s in exporter.get_finished_spans()}
    failed = next(s for out, s in spans.items() if "Exit Code: 128" in out)
    ok = next(s for out, s in spans.items() if "Exit Code: 0" in out)
    assert failed.attributes.get("langfuse.observation.level") == "ERROR"
    assert failed.attributes.get("langfuse.observation.status_message") == "fatal: not a git repository Exit Code: 128"
    assert '"status": "error"' in failed.attributes["langfuse.observation.output"]
    assert ok.attributes.get("langfuse.observation.level") in (None, "DEFAULT")
    client.shutdown()

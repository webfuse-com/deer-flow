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

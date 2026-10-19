"""[argus patch #149] The one-off backfill records the agent on old channel threads, and only there."""

import asyncio
import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import create_async_engine

from deerflow.persistence.thread_meta.model import ThreadMetaRow

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "argus_backfill_channel_thread_agent.py"
SPEC = importlib.util.spec_from_file_location("argus_backfill_channel_thread_agent", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
backfill = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backfill)

TABLE = ThreadMetaRow.__table__
STAMP = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)
TG = {"type": "im_channel", "provider": "telegram", "chat_id": "111"}
ROWS = {
    "tg-old": {"channel_source": {**TG, "topic_id": "sched:outreach-sync"}},
    "tg-done": {"channel_source": TG, "agent_name": "atlas"},
    "web": {"agent_name": "atlas"},
    "web-default": {},
    "pinned-coder": {"channel_source": TG, "channel_agent_name": "coder"},
    "pinned-lead": {"channel_source": TG, "channel_agent_name": "lead_agent"},
    "slack-old": {"channel_source": {"type": "im_channel", "provider": "slack", "chat_id": "C1"}, "title_hint": "kept"},
}


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'deerflow.db'}"


async def _seed(url: str) -> None:
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(TABLE.create)
        for thread_id, metadata in ROWS.items():
            await conn.execute(insert(TABLE).values(thread_id=thread_id, user_id="u1", status="idle", metadata_json=metadata, created_at=STAMP, updated_at=STAMP))
    await engine.dispose()


async def _rows(url: str) -> dict[str, tuple[dict, datetime]]:
    engine = create_async_engine(url)
    async with engine.connect() as conn:
        rows = (await conn.execute(select(TABLE.c.thread_id, TABLE.c.metadata_json, TABLE.c.updated_at))).all()
    await engine.dispose()
    return {thread_id: (metadata, updated_at) for thread_id, metadata, updated_at in rows}


def test_agent_for_picks_only_unrouted_channel_threads():
    assert backfill.agent_for(ROWS["tg-old"], "atlas") == "atlas"
    assert backfill.agent_for(ROWS["tg-done"], "atlas") is None
    assert backfill.agent_for(ROWS["web"], "atlas") is None
    assert backfill.agent_for(ROWS["web-default"], "atlas") is None
    assert backfill.agent_for(ROWS["pinned-coder"], "atlas") == "coder"
    assert backfill.agent_for(ROWS["pinned-lead"], "atlas") is None


def test_dry_run_counts_and_writes_nothing(tmp_path):
    url = _url(tmp_path)
    asyncio.run(_seed(url))
    before = asyncio.run(_rows(url))
    summary = asyncio.run(backfill.backfill(url, "", default_agent="atlas", apply=False, backup=tmp_path / "b.json"))
    assert summary["channel_threads_without_agent"] == 3
    assert summary["by_provider"] == {"telegram": 2, "slack": 1}
    assert summary["by_agent"] == {"atlas": 2, "coder": 1}
    assert asyncio.run(_rows(url)) == before
    assert not (tmp_path / "b.json").exists()


def test_apply_backs_up_updates_keeps_updated_at_and_is_idempotent(tmp_path):
    url = _url(tmp_path)
    asyncio.run(_seed(url))
    backup = tmp_path / "backups" / "b.json"
    summary = asyncio.run(backfill.backfill(url, "", default_agent="atlas", apply=True, backup=backup))
    assert summary["updated"] == 3 and summary["backup"] == str(backup)
    assert json.loads(backup.read_text()) == {"tg-old": ROWS["tg-old"], "pinned-coder": ROWS["pinned-coder"], "slack-old": ROWS["slack-old"]}

    after = asyncio.run(_rows(url))
    assert after["tg-old"][0] == {**ROWS["tg-old"], "agent_name": "atlas"}
    assert after["pinned-coder"][0] == {**ROWS["pinned-coder"], "agent_name": "coder"}
    assert after["slack-old"][0] == {**ROWS["slack-old"], "agent_name": "atlas"}
    for untouched in ("tg-done", "web", "web-default", "pinned-lead"):
        assert after[untouched][0] == ROWS[untouched]
    assert {updated_at.replace(tzinfo=UTC) if updated_at.tzinfo is None else updated_at for _, updated_at in after.values()} == {STAMP}

    again = asyncio.run(backfill.backfill(url, "", default_agent="atlas", apply=True, backup=tmp_path / "again.json"))
    assert again["channel_threads_without_agent"] == 0 and "updated" not in again
    assert not (tmp_path / "again.json").exists()


def test_provider_filter(tmp_path):
    url = _url(tmp_path)
    asyncio.run(_seed(url))
    summary = asyncio.run(backfill.backfill(url, "", default_agent="atlas", apply=False, backup=None, provider="slack"))
    assert summary["channel_threads_without_agent"] == 1 and summary["sample"] == ["slack-old"]


def test_cli_refuses_the_lead_agent(capsys):
    import pytest

    with pytest.raises(SystemExit):
        backfill.run(["--agent", "lead_agent", "--url", "sqlite+aiosqlite:///:memory:"])


def test_apply_on_postgres_json_column():
    """Opt-in (CI sets TEST_POSTGRES_URI): the row lock and the JSON update on the real dialect."""
    import os
    import uuid

    import pytest
    import sqlalchemy as sa
    from support.postgres import asyncpg_test_url

    from deerflow.persistence.postgres_schema import build_asyncpg_connect_args

    uri = os.environ.get("TEST_POSTGRES_URI")
    if not uri:
        pytest.skip("requires TEST_POSTGRES_URI (real Postgres threads_meta)")
    url = asyncpg_test_url(uri)
    schema = f"tmb_{uuid.uuid4().hex}"

    async def go():
        engine = create_async_engine(url, connect_args=build_asyncpg_connect_args(schema))
        try:
            async with engine.begin() as conn:
                await conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
                await conn.run_sync(TABLE.create)
                for thread_id, metadata in ROWS.items():
                    await conn.execute(insert(TABLE).values(thread_id=thread_id, user_id="u1", status="idle", metadata_json=metadata, created_at=STAMP, updated_at=STAMP))
            summary = await backfill.backfill(url, schema, default_agent="atlas", apply=True, backup=None)
            assert summary["updated"] == 3
            async with engine.connect() as conn:
                rows = {t: (m, u) for t, m, u in (await conn.execute(select(TABLE.c.thread_id, TABLE.c.metadata_json, TABLE.c.updated_at))).all()}
            assert rows["tg-old"][0]["agent_name"] == "atlas" and rows["pinned-coder"][0]["agent_name"] == "coder"
            assert rows["web"][0] == ROWS["web"]
            assert {u for _, u in rows.values()} == {STAMP}
        finally:
            async with engine.begin() as conn:
                await conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await engine.dispose()

    asyncio.run(go())

#!/usr/bin/env python3
"""[argus patch #149] One-off backfill: record the agent on channel threads created before #149.

Threads a channel created (Telegram chats, scheduled fires) carried only
``channel_source`` metadata, so opening one in the browser routed to
``/workspace/chats/<id>`` and ran the default agent without the citizen's SOUL
or the Atlas base prompt. Patch #149 records ``agent_name`` on new channel
threads; this script adds it to existing ``threads_meta`` rows that have
``channel_source`` and no ``agent_name``. A thread pinned with ``/agent use``
(``channel_agent_name``) gets that agent; one pinned to ``lead_agent`` is left
alone; every other row gets ``--agent``. ``updated_at`` is kept, so the
sidebar order does not change.

Run it inside the stack's gateway container, which connects as the stack's
own database role (``DEERFLOW_PG_DSN``, ``search_path`` pinned to its
``deerflow_<slug>`` schema). Dry run first:

    podman exec argus-atlas-<name>-gateway sh -c \\
      'cd /app/backend && PYTHONPATH=. .venv/bin/python scripts/argus_backfill_channel_thread_agent.py --agent atlas'
    podman exec argus-atlas-<name>-gateway sh -c \\
      'cd /app/backend && PYTHONPATH=. .venv/bin/python scripts/argus_backfill_channel_thread_agent.py --agent atlas --apply'

``--apply`` first writes every affected row's original metadata to
``$DEER_FLOW_HOME/backups/threads-meta-agent-backfill-<UTC>.json`` (override
with ``--backup``), then updates all rows in one transaction, re-reading each
row under a row lock. Undo: write each saved ``metadata_json`` back.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

THREAD_AGENT_METADATA_KEY = "agent_name"
CHANNEL_AGENT_METADATA_KEY = "channel_agent_name"
LEAD_AGENT = "lead_agent"


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def agent_for(metadata: dict[str, Any], default_agent: str) -> str | None:
    """The agent to record for a channel thread, or None to leave the row alone."""
    if not isinstance(metadata.get("channel_source"), dict):
        return None
    current = metadata.get(THREAD_AGENT_METADATA_KEY)
    if isinstance(current, str) and current.strip():
        return None
    pinned = metadata.get(CHANNEL_AGENT_METADATA_KEY)
    if isinstance(pinned, str) and pinned.strip():
        return None if pinned.strip().lower() == LEAD_AGENT else pinned.strip()
    return default_agent


def _write_backup(path: Path, rows: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=1))


def _database_target(args: argparse.Namespace) -> tuple[str, str]:
    if args.url:
        return args.url, args.schema or ""
    try:
        from deerflow.config.app_config import get_app_config

        database = get_app_config().database
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"Could not read the gateway's database config ({exc}); pass --url explicitly.")
    if database.backend == "memory":
        sys.exit("database.backend is memory: nothing persisted to backfill.")
    schema = database.postgres_schema if database.backend == "postgres" else ""
    return database.app_sqlalchemy_url, schema


def _engine(url: str, schema: str):
    from sqlalchemy.ext.asyncio import create_async_engine

    if url.startswith("postgresql"):
        from deerflow.persistence.postgres_schema import build_asyncpg_connect_args

        return create_async_engine(url, connect_args=build_asyncpg_connect_args(schema))
    return create_async_engine(url)


async def backfill(url: str, schema: str, *, default_agent: str, apply: bool, backup: Path | None, provider: str | None = None) -> dict[str, Any]:
    from sqlalchemy import select, update

    from deerflow.persistence.thread_meta.model import ThreadMetaRow

    table = ThreadMetaRow.__table__
    engine = _engine(url, schema)
    try:
        async with engine.connect() as conn:
            rows = (await conn.execute(select(table.c.thread_id, table.c.metadata_json))).all()
        candidates: list[tuple[str, dict[str, Any], str]] = []
        for thread_id, raw in rows:
            metadata = _as_dict(raw)
            agent = agent_for(metadata, default_agent)
            if agent is None:
                continue
            if provider and metadata["channel_source"].get("provider") != provider:
                continue
            candidates.append((thread_id, metadata, agent))
        summary: dict[str, Any] = {
            "threads": len(rows),
            "channel_threads_without_agent": len(candidates),
            "by_provider": dict(Counter(str(m["channel_source"].get("provider")) for _, m, _ in candidates)),
            "by_agent": dict(Counter(agent for _, _, agent in candidates)),
            "sample": [thread_id for thread_id, _, _ in candidates[:10]],
            "apply": apply,
        }
        if not apply or not candidates:
            return summary
        if backup is not None:
            await asyncio.to_thread(_write_backup, backup, {thread_id: metadata for thread_id, metadata, _ in candidates})
            summary["backup"] = str(backup)
        updated = 0
        async with engine.begin() as conn:
            for thread_id, _seen, agent in candidates:
                current_raw = (await conn.execute(select(table.c.metadata_json).where(table.c.thread_id == thread_id).with_for_update())).scalar_one_or_none()
                current = _as_dict(current_raw)
                if agent_for(current, agent) != agent:
                    continue  # changed since the scan (or gone): leave it
                await conn.execute(
                    update(table)
                    .where(table.c.thread_id == thread_id)
                    # Setting updated_at to itself keeps the ORM onupdate from
                    # moving every backfilled thread to the top of the sidebar.
                    .values(metadata_json={**current, THREAD_AGENT_METADATA_KEY: agent}, updated_at=table.c.updated_at)
                )
                updated += 1
        summary["updated"] = updated
        return summary
    finally:
        await engine.dispose()


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--agent", required=True, help="agent recorded on unpinned channel threads (Atlas stacks: atlas)")
    parser.add_argument("--apply", action="store_true", help="write the backfill (default: dry run)")
    parser.add_argument("--provider", help="only threads from this channel (telegram, slack, ...)")
    parser.add_argument("--backup", help="where --apply saves the original metadata first")
    parser.add_argument("--url", help="SQLAlchemy async URL (default: the gateway's database config)")
    parser.add_argument("--schema", help="postgres search_path with --url")
    args = parser.parse_args(argv)
    if not args.agent.strip() or args.agent.strip().lower() == LEAD_AGENT:
        parser.error("--agent must name a custom agent")
    url, schema = _database_target(args)
    if args.backup:
        backup = Path(args.backup)
    else:
        home = Path(os.environ.get("DEER_FLOW_HOME") or ".deer-flow")
        backup = home / "backups" / f"threads-meta-agent-backfill-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    summary = asyncio.run(backfill(url, schema, default_agent=args.agent.strip(), apply=args.apply, backup=backup, provider=args.provider))
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())

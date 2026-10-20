#!/usr/bin/env python3
"""[argus patch #149] One-off backfill: record the agent on channel threads created before #149.

Threads a channel created (Telegram chats, scheduled fires) carried only
``channel_source`` metadata, so opening one in the browser routed to
``/workspace/chats/<id>`` and ran the default agent without the citizen's SOUL
or the Atlas base prompt. Patch #149 records ``agent_name`` on new channel
threads; this script adds it to existing ``threads_meta`` rows that have
``channel_source`` and no ``agent_name``. A thread pinned with ``/agent use``
(``channel_agent_name``) gets that agent; one pinned to ``lead_agent`` is left
alone; every other row gets the agent its latest run actually used (the run's
``context.agent_name`` or ``config.configurable.agent_name``, the value the
worker also stamps on the run's checkpoints), so a pythia-ext or
pythia-internal channel thread is not relabelled Atlas. A thread whose agent
cannot be determined (no run, or a run on the default agent) is left alone and
listed under ``undetermined``. ``--agent`` limits the write to threads whose
agent is that one. ``updated_at`` is kept, so the sidebar order does not
change.

Run it inside the stack's gateway container, which connects as the stack's
own database role (``DEERFLOW_PG_DSN``, ``search_path`` pinned to its
``deerflow_<slug>`` schema). Dry run first:

    podman exec argus-atlas-<name>-gateway sh -c \\
      'cd /app/backend && PYTHONPATH=. .venv/bin/python scripts/argus_backfill_channel_thread_agent.py'
    podman exec argus-atlas-<name>-gateway sh -c \\
      'cd /app/backend && PYTHONPATH=. .venv/bin/python scripts/argus_backfill_channel_thread_agent.py --apply'

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


def agent_for(metadata: dict[str, Any], run_agent: str | None) -> str | None:
    """The agent to record for a channel thread, or None to leave the row alone.

    *run_agent* is the agent the thread's latest run used (None when unknown).
    """
    if not isinstance(metadata.get("channel_source"), dict):
        return None
    current = metadata.get(THREAD_AGENT_METADATA_KEY)
    if isinstance(current, str) and current.strip():
        return None
    pinned = metadata.get(CHANNEL_AGENT_METADATA_KEY)
    if isinstance(pinned, str) and pinned.strip():
        return None if pinned.strip().lower() == LEAD_AGENT else pinned.strip()
    return run_agent


def needs_agent(metadata: dict[str, Any]) -> bool:
    """A channel thread with no recorded agent and no lead_agent pin."""
    return agent_for(metadata, "?") is not None


def run_agent_name(assistant_id: Any, kwargs: Any) -> str | None:
    """The custom agent a run used, or None for the default agent or an unreadable row."""
    kwargs = _as_dict(kwargs)
    config = kwargs.get("config") if isinstance(kwargs.get("config"), dict) else {}
    for source in (kwargs.get("context"), config.get("configurable"), config.get("context")):
        name = source.get("agent_name") if isinstance(source, dict) else None
        if isinstance(name, str) and name.strip():
            return None if name.strip().lower() == LEAD_AGENT else name.strip()
    if isinstance(assistant_id, str) and assistant_id.strip() and assistant_id.strip().lower() != LEAD_AGENT:
        return assistant_id.strip()
    return None


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


async def backfill(url: str, schema: str, *, only_agent: str | None = None, apply: bool, backup: Path | None, provider: str | None = None) -> dict[str, Any]:
    from sqlalchemy import select, update

    from deerflow.persistence.run.model import RunRow
    from deerflow.persistence.thread_meta.model import ThreadMetaRow

    table = ThreadMetaRow.__table__
    runs = RunRow.__table__
    engine = _engine(url, schema)
    try:
        candidates: list[tuple[str, dict[str, Any], str]] = []
        undetermined: list[str] = []
        other_agent: Counter[str] = Counter()
        async with engine.connect() as conn:
            rows = (await conn.execute(select(table.c.thread_id, table.c.metadata_json))).all()
            for thread_id, raw in rows:
                metadata = _as_dict(raw)
                if not needs_agent(metadata):
                    continue
                if provider and metadata["channel_source"].get("provider") != provider:
                    continue
                latest = (await conn.execute(select(runs.c.assistant_id, runs.c.kwargs_json).where(runs.c.thread_id == thread_id).order_by(runs.c.created_at.desc()).limit(1))).first()
                agent = agent_for(metadata, run_agent_name(latest[0], latest[1]) if latest is not None else None)
                if agent is None:
                    undetermined.append(thread_id)
                    continue
                if only_agent and agent != only_agent:
                    other_agent[agent] += 1
                    continue
                candidates.append((thread_id, metadata, agent))
        summary: dict[str, Any] = {
            "threads": len(rows),
            "channel_threads_without_agent": len(candidates),
            "by_provider": dict(Counter(str(m["channel_source"].get("provider")) for _, m, _ in candidates)),
            "by_agent": dict(Counter(agent for _, _, agent in candidates)),
            "sample": [thread_id for thread_id, _, _ in candidates[:10]],
            "undetermined": len(undetermined),
            "undetermined_sample": undetermined[:20],
            "apply": apply,
        }
        if other_agent:
            summary["skipped_other_agent"] = dict(other_agent)
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
    parser.add_argument("--agent", help="only record this agent (threads whose latest run used another are skipped and counted)")
    parser.add_argument("--apply", action="store_true", help="write the backfill (default: dry run)")
    parser.add_argument("--provider", help="only threads from this channel (telegram, slack, ...)")
    parser.add_argument("--backup", help="where --apply saves the original metadata first")
    parser.add_argument("--url", help="SQLAlchemy async URL (default: the gateway's database config)")
    parser.add_argument("--schema", help="postgres search_path with --url")
    args = parser.parse_args(argv)
    if args.agent is not None and (not args.agent.strip() or args.agent.strip().lower() == LEAD_AGENT):
        parser.error("--agent must name a custom agent")
    url, schema = _database_target(args)
    if args.backup:
        backup = Path(args.backup)
    else:
        home = Path(os.environ.get("DEER_FLOW_HOME") or ".deer-flow")
        backup = home / "backups" / f"threads-meta-agent-backfill-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    summary = asyncio.run(backfill(url, schema, only_agent=args.agent.strip() if args.agent else None, apply=args.apply, backup=backup, provider=args.provider))
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())

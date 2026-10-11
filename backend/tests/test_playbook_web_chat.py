"""[argus patch #120] A scheduled playbook run in a web chat (new each run, or an existing one)."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import app.gateway.internal_auth as internal_auth
import app.gateway.routers.playbooks as playbooks
from app.gateway.routers.playbooks import router
from app.gateway.scheduled_task_validation import context_modes_for, validate_scheduled_task_create, validate_scheduled_task_update

TOKEN = "test-internal-token"
OWNER = "owner-1"
REPORT = "http://argus-scheduler:8000/api/runs/41/output"


@pytest.fixture()
def client(tmp_path: Path):
    (tmp_path / "morning-briefing.md").write_text("Good morning.", encoding="utf-8")
    app = FastAPI()
    app.include_router(router)
    users = MagicMock()
    users.get_user_by_email = AsyncMock(side_effect=lambda email: SimpleNamespace(id=OWNER) if email == "owner@example.com" else None)
    store = MagicMock()
    store.check_access = AsyncMock(side_effect=lambda thread_id, user_id, require_existing: thread_id == "thread-mine" and user_id == OWNER)
    launch = AsyncMock(return_value={"run_id": "r1", "thread_id": "t"})
    with (
        patch.object(internal_auth, "_INTERNAL_AUTH_TOKEN", TOKEN),
        patch.object(playbooks, "PLAYBOOK_DIR", tmp_path),
        patch("app.gateway.deps.get_user_repository", return_value=users),
        patch("app.gateway.deps.get_thread_store", return_value=store),
        patch("app.gateway.services.launch_scheduled_thread_run", launch),
        TestClient(app, raise_server_exceptions=False) as c,
    ):
        c.launch = launch
        yield c


def fire(client, **body):
    return client.post("/api/playbooks/morning-briefing/fire", json=body, headers={internal_auth.INTERNAL_AUTH_HEADER_NAME: TOKEN})


def test_a_fresh_web_chat_each_run(client):
    resp = fire(client, fresh=True, owner_email="Owner@Example.com", report_url=REPORT, trigger="manual", timezone="Europe/Amsterdam", allowed_tools=["pythia_query"], memory="read-only")
    assert resp.status_code == 200 and resp.json()["channel"] == "web"
    kwargs = client.launch.await_args.kwargs
    assert kwargs["owner_user_id"] == OWNER and kwargs["assistant_id"] == "atlas"
    assert kwargs["prompt"] == "Good morning." and kwargs["title"].startswith("Morning briefing · ")
    assert len(kwargs["thread_id"]) == 36  # a new chat
    assert kwargs["origin"]["task_id"] == "morning-briefing" and kwargs["origin"]["task_run_id"] == "41"
    assert kwargs["origin"]["trigger"] == "manual"
    assert kwargs["context"] == {"unattended": True, "memory_mode": "read-only", "allowed_tools": ["pythia_query"]}
    assert kwargs["metadata"] == {"argus_schedule_id": "morning-briefing", "argus_report_url": REPORT}


def test_an_existing_chat_of_the_owner_only(client):
    assert fire(client, thread_id="thread-mine", owner_email="owner@example.com").status_code == 200
    kwargs = client.launch.await_args.kwargs
    assert kwargs["thread_id"] == "thread-mine" and kwargs["title"] is None
    assert fire(client, thread_id="thread-other", owner_email="owner@example.com").status_code == 404


def test_a_web_chat_needs_a_known_owner(client):
    assert fire(client, fresh=True).status_code == 422
    assert fire(client, fresh=True, owner_email="nobody@example.com").status_code == 409
    assert client.launch.await_count == 0


def test_a_busy_chat_is_retried_and_reported_when_it_stays_busy(client, monkeypatch):
    client.launch.side_effect = HTTPException(status_code=409, detail="busy")
    reported = AsyncMock()
    monkeypatch.setattr(playbooks, "_BUSY_RETRY_SECONDS", 0)
    monkeypatch.setattr(playbooks, "_BUSY_RETRY_LIMIT", 2)
    with patch("app.channels._delivery_report.report_delivery", reported):
        assert fire(client, thread_id="thread-mine", owner_email="owner@example.com", report_url=REPORT).status_code == 200
        # the retries run on the test client's event loop thread
        for _ in range(100):
            if reported.await_count:
                break
            time.sleep(0.02)
    assert client.launch.await_count == 3
    assert reported.await_args.kwargs["status"] == "failed" and reported.await_args.kwargs["thread_id"] == "thread-mine"


class _Backend:
    channels = ["telegram"]


@pytest.mark.asyncio
async def test_a_backend_channel_is_a_context_mode():
    modes = context_modes_for(_Backend())
    assert modes == {"fresh_thread_per_run", "reuse_thread", "channel:telegram"}
    assert context_modes_for(None) == {"fresh_thread_per_run", "reuse_thread"}

    body = SimpleNamespace(
        context_mode="channel:telegram",
        thread_id="x",
        schedule_type="cron",
        schedule_spec={"cron": "0 9 * * *"},
        timezone="UTC",
        assistant_id=None,
        stop_condition=None,
        goal_objective=None,
        max_runs=None,
        end_at=None,
        title="t",
        prompt="p",
    )

    async def resolver(raw, user_id):
        return "lead_agent"

    config = SimpleNamespace(min_once_delay_seconds=60)
    definition = await validate_scheduled_task_create(
        body,
        user_id="u",
        thread_store=MagicMock(),
        scheduler_config=config,
        assistant_resolver=resolver,
        context_modes=modes,
    )
    assert definition["context_mode"] == "channel:telegram" and definition["thread_id"] is None
    with pytest.raises(HTTPException):
        await validate_scheduled_task_create(body, user_id="u", thread_store=MagicMock(), scheduler_config=config, assistant_resolver=resolver)
    existing = {"context_mode": "fresh_thread_per_run", "thread_id": None, "schedule_type": "cron", "schedule_spec": {"cron": "0 9 * * *"}, "timezone": "UTC"}
    updates = await validate_scheduled_task_update(
        existing,
        {"context_mode": "channel:telegram"},
        user_id="u",
        thread_store=MagicMock(),
        scheduler_config=config,
        assistant_resolver=resolver,
        used=0,
        now=datetime.now(UTC),
        context_modes=modes,
    )
    assert updates["context_mode"] == "channel:telegram" and updates["thread_id"] is None

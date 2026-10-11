"""[argus patch #115] scheduler.backend_class: an external scheduler behind the Scheduled tasks API."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest
from _router_auth_helpers import make_authed_test_app
from fastapi.testclient import TestClient

from app.gateway.auth.models import User
from app.gateway.deps import get_config, load_scheduler_backend
from app.gateway.routers import features, scheduled_tasks
from deerflow.config.scheduler_config import SchedulerConfig
from deerflow.scheduler.runtime import scheduler_tools_enabled

_OWNER = User(id=UUID("12345678-1234-1234-1234-123456789abc"), email="owner@example.com", password_hash="unused", system_role="user")
OWNER = str(_OWNER.id)

TASK = {
    "id": "sched-morning-brief",
    "user_id": OWNER,
    "title": "Morning briefing",
    "prompt": "Summarize my day.",
    "status": "enabled",
    "schedule_type": "cron",
    "schedule_spec": {"cron": "15 7 * * 1-5"},
    "timezone": "Europe/Amsterdam",
    "context_mode": "reuse_thread",
    "thread_id": "thread-brief",
    "origin_thread_id": None,
    "assistant_id": "atlas",
    "next_run_at": "2026-10-12T05:15:00+00:00",
    "run_count": 3,
}


class FakeTaskRepo:
    def __init__(self) -> None:
        self.tasks = {TASK["id"]: dict(TASK)}

    async def list_by_user(self, user_id):
        return [dict(t) for t in self.tasks.values() if t["user_id"] == user_id]

    async def get(self, task_id, *, user_id=None):
        task = self.tasks.get(task_id)
        return dict(task) if task and (user_id is None or task["user_id"] == user_id) else None

    async def list_by_user_and_thread(self, user_id, thread_id):
        return [{**t, "thread_relation": "reuse"} for t in self.tasks.values() if t["user_id"] == user_id and t["thread_id"] == thread_id]

    async def automatic_runs_used_for(self, ids):
        return {}

    async def active_run_status_for(self, ids):
        return {}

    async def get_active_run_status(self, task_id):
        return None


class FakeRunRepo:
    async def list_by_task(self, task_id, *, limit, offset, status=None):
        return [{"id": "run-1", "thread_id": "thread-brief", "status": "success", "trigger": "scheduled"}]


class FakeEventRepo:
    async def list_for_thread(self, *, user_id, thread_id, limit):
        return []


class FakeService:
    is_running = True

    def __init__(self) -> None:
        self.dispatched = []

    async def dispatch_task(self, task, *, now, trigger):
        self.dispatched.append((task["id"], trigger))
        return {"outcome": "launched", "thread_id": task["thread_id"]}

    async def handle_run_completion(self, *args, **kwargs):
        return None


class FakeBackend:
    supports = {"create": False, "edit": False, "delete": False}

    def __init__(self, config) -> None:
        self.config = config
        self.task_repo = FakeTaskRepo()
        self.run_repo = FakeRunRepo()
        self.event_repo = FakeEventRepo()
        self.service = FakeService()


class Incomplete:
    def __init__(self, config) -> None:
        self.task_repo = FakeTaskRepo()


def _config(**scheduler):
    return SimpleNamespace(
        scheduler=SchedulerConfig(**scheduler),
        agents_api=SimpleNamespace(enabled=True),
        tools=[],
        subagent_runtime=SimpleNamespace(max_running=3),
        knowledge_base=SimpleNamespace(enabled=False, scope_selection_enabled=False),
        capability_center=SimpleNamespace(enabled=True),
        get_tool_config=lambda name: None,
    )


def test_no_backend_by_default():
    assert load_scheduler_backend(_config()) is None


def test_a_backend_must_provide_every_part():
    with pytest.raises(ValueError, match="has no run_repo"):
        load_scheduler_backend(_config(backend_class="test_scheduler_backend_class:Incomplete"))


def test_chat_schedule_tools_stay_off_with_a_backend():
    assert scheduler_tools_enabled(_config(enabled=True, tool_enabled=True)) is True
    assert scheduler_tools_enabled(_config(enabled=True, tool_enabled=True, backend_class="test_scheduler_backend_class:FakeBackend")) is False


@pytest.fixture
def client(monkeypatch):
    config = _config(backend_class="test_scheduler_backend_class:FakeBackend", tool_enabled=True)
    backend = load_scheduler_backend(config)
    app = make_authed_test_app(user_factory=lambda: _OWNER, bind_current_user=True)
    # What deps.py and app.py do at startup.
    app.state.scheduled_task_backend = backend
    app.state.scheduled_task_repo = backend.task_repo
    app.state.scheduled_task_run_repo = backend.run_repo
    app.state.scheduled_task_event_repo = backend.event_repo
    app.state.scheduled_task_service = backend.service
    app.include_router(scheduled_tasks.router)
    app.include_router(features.router)
    app.dependency_overrides[get_config] = lambda: config
    monkeypatch.setattr(scheduled_tasks, "get_config", lambda: config)

    async def owner(_request):
        return _OWNER

    monkeypatch.setattr(scheduled_tasks, "get_optional_user_from_request", owner)
    with TestClient(app) as c:
        yield c, backend


def test_the_page_lists_and_runs_the_backends_tasks(client):
    c, backend = client
    listed = c.get("/api/scheduled-tasks").json()
    assert [t["id"] for t in listed] == ["sched-morning-brief"] and listed[0]["automatic_runs_used"] == 0
    assert c.get("/api/scheduled-tasks/sched-morning-brief").json()["title"] == "Morning briefing"
    assert c.get("/api/threads/thread-brief/scheduled-tasks").json()[0]["thread_relation"] == "reuse"
    assert c.get("/api/scheduled-tasks/sched-morning-brief/runs").json()[0]["thread_id"] == "thread-brief"
    assert c.get("/api/threads/thread-brief/scheduled-task-events").json() == {"events": []}
    run = c.post("/api/scheduled-tasks/sched-morning-brief/trigger").json()
    assert run["outcome"] == "launched" and run["thread_id"] == "thread-brief"
    assert backend.service.dispatched == [("sched-morning-brief", "manual")]


def test_features_report_the_backends_offer(client):
    c, _ = client
    feature = c.get("/api/features").json()["scheduled_tasks"]
    assert feature["available"] is True and feature["running"] is True
    assert feature["tool_enabled"] is False  # the chat tools drive DeerFlow's own scheduler
    assert feature["external"] is True
    assert feature["supports"] == {
        "goals": False,
        "stop_condition": False,
        "limits": False,
        "interval": False,
        "notes": False,
        "create": False,
        "edit": False,
        "delete": False,
        "once": False,
        "context": False,
    }

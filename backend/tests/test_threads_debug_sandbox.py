"""Tests for the per-thread debug-sandbox endpoint.

``POST /api/threads/{id}/debug-sandbox`` acquires (or re-acquires) the
thread's AIO sandbox on demand and returns the relative URL that the
per-project nginx ``/debug-sandbox/<hash>/`` location proxies to. See
``app/gateway/routers/threads.py::acquire_debug_sandbox`` and the argus
nginx overlay.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from _router_auth_helpers import make_authed_test_app
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from app.gateway.routers import threads


def _build_app() -> FastAPI:
    """Stub-authed app including only the threads router.

    The permissive stub thread_store in ``_router_auth_helpers`` always
    passes ``check_access`` (including ``require_existing=True``), so the
    ``@require_permission`` decorator on the endpoint resolves and we can
    drive the acquire logic directly.
    """
    app = make_authed_test_app()
    app.state.store = InMemoryStore()
    app.state.checkpointer = InMemorySaver()
    app.include_router(threads.router)
    return app


# ── unit: the container-id classifier ────────────────────────────────────


def test_is_container_sandbox_id_accepts_deterministic_hash():
    # sha256(thread_id)[:8] is exactly 8 lowercase hex chars.
    assert threads._is_container_sandbox_id("fc7b6e5e") is True
    assert threads._is_container_sandbox_id("00000000") is True


def test_is_container_sandbox_id_rejects_local_provider_ids():
    assert threads._is_container_sandbox_id("local") is False
    assert threads._is_container_sandbox_id("local:some-thread-id") is False
    assert threads._is_container_sandbox_id("") is False
    # Wrong length / non-hex must not be mistaken for a container hash.
    assert threads._is_container_sandbox_id("fc7b6e5") is False
    assert threads._is_container_sandbox_id("fc7b6e5ee") is False
    assert threads._is_container_sandbox_id("ZZZZZZZZ") is False


# ── endpoint behavior ────────────────────────────────────────────────────


def test_acquire_debug_sandbox_returns_hash_and_url():
    app = _build_app()
    provider = AsyncMock()
    provider.acquire_async.return_value = "fc7b6e5e"

    with patch("deerflow.sandbox.sandbox_provider.get_sandbox_provider", return_value=provider):
        with TestClient(app) as client:
            resp = client.post("/api/threads/c226c7e0-1c76-469b-b60b-9c1ea486b8ae/debug-sandbox")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"hash": "fc7b6e5e", "url": "/debug-sandbox/fc7b6e5e/"}
    provider.acquire_async.assert_awaited_once_with("c226c7e0-1c76-469b-b60b-9c1ea486b8ae")


def test_acquire_debug_sandbox_rejects_local_provider_with_409():
    app = _build_app()
    provider = AsyncMock()
    provider.acquire_async.return_value = "local:c226c7e0"

    with patch("deerflow.sandbox.sandbox_provider.get_sandbox_provider", return_value=provider):
        with TestClient(app) as client:
            resp = client.post("/api/threads/c226c7e0/debug-sandbox")

    assert resp.status_code == 409


def test_acquire_debug_sandbox_maps_acquire_failure_to_502():
    app = _build_app()
    provider = AsyncMock()
    provider.acquire_async.side_effect = RuntimeError("docker daemon unreachable")

    with patch("deerflow.sandbox.sandbox_provider.get_sandbox_provider", return_value=provider):
        with TestClient(app) as client:
            resp = client.post("/api/threads/c226c7e0/debug-sandbox")

    assert resp.status_code == 502

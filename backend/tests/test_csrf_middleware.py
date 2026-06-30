"""Tests for CSRF middleware."""

from fastapi import FastAPI
from starlette.testclient import TestClient

from app.gateway.csrf_middleware import CSRFMiddleware


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(CSRFMiddleware)

    @app.post("/api/v1/auth/login/local")
    async def login_local():
        return {"ok": True}

    @app.post("/api/v1/auth/register")
    async def register():
        return {"ok": True}

    @app.post("/api/threads/abc/runs/stream")
    async def protected_mutation():
        return {"ok": True}

    return app


def test_auth_post_rejects_cross_origin_browser_request():
    """CSRF-exempt auth routes must not accept hostile browser origins.

    Login/register endpoints intentionally skip the double-submit token because
    first-time callers do not have a token yet. They still set an auth session,
    so a hostile cross-site form POST must be rejected to avoid login CSRF /
    session fixation.
    """
    client = TestClient(_make_app(), base_url="https://deerflow.example")

    response = client.post(
        "/api/v1/auth/login/local",
        headers={"Origin": "https://evil.example"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Cross-site auth request denied."


def test_auth_post_allows_same_origin_browser_request():
    client = TestClient(_make_app(), base_url="https://deerflow.example")

    response = client.post(
        "/api/v1/auth/login/local",
        headers={"Origin": "https://deerflow.example"},
    )

    assert response.status_code == 200
    assert response.cookies.get("csrf_token")


def test_auth_post_rejects_malformed_origin_with_path():
    client = TestClient(_make_app(), base_url="https://deerflow.example")

    response = client.post(
        "/api/v1/auth/login/local",
        headers={"Origin": "https://deerflow.example/path"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Cross-site auth request denied."
    assert response.cookies.get("csrf_token") is None


def test_auth_post_rejects_malformed_origin_with_invalid_port():
    client = TestClient(_make_app(), base_url="https://deerflow.example")

    response = client.post(
        "/api/v1/auth/login/local",
        headers={"Origin": "https://deerflow.example:bad"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Cross-site auth request denied."
    assert response.cookies.get("csrf_token") is None


def test_auth_post_allows_same_origin_default_port_equivalence():
    client = TestClient(_make_app(), base_url="https://deerflow.example")

    response = client.post(
        "/api/v1/auth/login/local",
        headers={"Origin": "https://deerflow.example:443"},
    )

    assert response.status_code == 200
    assert response.cookies.get("csrf_token")


def test_auth_post_allows_forwarded_same_origin():
    client = TestClient(_make_app(), base_url="http://internal:8000")

    response = client.post(
        "/api/v1/auth/login/local",
        headers={
            "Origin": "https://deerflow.example",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "deerflow.example, internal:8000",
        },
    )

    assert response.status_code == 200
    assert response.cookies.get("csrf_token")


def test_auth_post_allows_forwarded_same_origin_with_non_default_port():
    client = TestClient(_make_app(), base_url="http://internal:8000")

    response = client.post(
        "/api/v1/auth/login/local",
        headers={
            "Origin": "http://localhost:2026",
            "X-Forwarded-Proto": "http",
            "X-Forwarded-Host": "localhost:2026",
        },
    )

    assert response.status_code == 200
    assert response.cookies.get("csrf_token")


def test_auth_post_allows_rfc_forwarded_same_origin():
    client = TestClient(_make_app(), base_url="http://internal:8000")

    response = client.post(
        "/api/v1/auth/login/local",
        headers={
            "Origin": "https://deerflow.example",
            "Forwarded": "proto=https;host=deerflow.example",
        },
    )

    assert response.status_code == 200
    assert response.cookies.get("csrf_token")
    assert "secure" in response.headers["set-cookie"].lower()


def test_auth_post_allows_explicit_configured_origin(monkeypatch):
    monkeypatch.setenv("GATEWAY_CORS_ORIGINS", "https://app.example")
    client = TestClient(_make_app(), base_url="https://api.example")

    response = client.post(
        "/api/v1/auth/register",
        headers={"Origin": "https://app.example"},
    )

    assert response.status_code == 200
    assert response.cookies.get("csrf_token")


def test_auth_post_does_not_treat_wildcard_cors_as_allowed_origin(monkeypatch):
    monkeypatch.setenv("GATEWAY_CORS_ORIGINS", "*")
    client = TestClient(_make_app(), base_url="https://api.example")

    response = client.post(
        "/api/v1/auth/login/local",
        headers={"Origin": "https://evil.example"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Cross-site auth request denied."


def test_auth_post_sets_strict_samesite_csrf_cookie():
    client = TestClient(_make_app(), base_url="https://deerflow.example")

    response = client.post(
        "/api/v1/auth/login/local",
        headers={"Origin": "https://deerflow.example"},
    )

    assert response.status_code == 200
    set_cookie = response.headers["set-cookie"].lower()
    assert "csrf_token=" in set_cookie
    assert "samesite=strict" in set_cookie
    assert "secure" in set_cookie


def test_auth_post_without_origin_still_allows_non_browser_clients():
    client = TestClient(_make_app(), base_url="https://deerflow.example")

    response = client.post("/api/v1/auth/login/local")

    assert response.status_code == 200
    assert response.cookies.get("csrf_token")


def test_non_auth_mutation_still_requires_double_submit_token():
    client = TestClient(_make_app(), base_url="https://deerflow.example")

    response = client.post(
        "/api/threads/abc/runs/stream",
        headers={"Origin": "https://deerflow.example"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "CSRF token missing. Include X-CSRF-Token header."


def test_non_auth_mutation_allows_valid_double_submit_token():
    client = TestClient(_make_app(), base_url="https://deerflow.example")
    client.cookies.set("csrf_token", "known-token")

    response = client.post(
        "/api/threads/abc/runs/stream",
        headers={
            "Origin": "https://deerflow.example",
            "X-CSRF-Token": "known-token",
        },
    )

    assert response.status_code == 200


def test_non_auth_mutation_rejects_mismatched_double_submit_token():
    client = TestClient(_make_app(), base_url="https://deerflow.example")
    client.cookies.set("csrf_token", "cookie-token")

    response = client.post(
        "/api/threads/abc/runs/stream",
        headers={
            "Origin": "https://deerflow.example",
            "X-CSRF-Token": "header-token",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "CSRF token mismatch."


def test_channel_posts_require_double_submit_csrf():
    client = TestClient(_make_app(), base_url="https://deerflow.example")

    response = client.post(
        "/api/channels/slack/connect",
        headers={"Origin": "https://deerflow.example"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "CSRF token missing. Include X-CSRF-Token header."


# ---------------------------------------------------------------------------
# [argus patch #30] internal-token CSRF exemption
# ---------------------------------------------------------------------------


def test_internal_token_exempts_csrf(monkeypatch):
    """A request bearing a valid internal-auth token skips CSRF entirely — no
    cookie pair needed. The token is a strictly stronger guard than CSRF, and
    Chronos fires /api/playbooks/<id>/fire with it (no browser cookies)."""
    import app.gateway.internal_auth as internal_auth

    monkeypatch.setattr(internal_auth, "_INTERNAL_AUTH_TOKEN", "the-internal-token")
    client = TestClient(_make_app(), base_url="https://deerflow.example")

    response = client.post(
        "/api/threads/abc/runs/stream",
        headers={
            "Origin": "https://deerflow.example",
            internal_auth.INTERNAL_AUTH_HEADER_NAME: "the-internal-token",
        },
    )

    assert response.status_code == 200


def test_wrong_internal_token_still_requires_csrf(monkeypatch):
    """An invalid internal token is NOT an exemption — CSRF is not weakened."""
    import app.gateway.internal_auth as internal_auth

    monkeypatch.setattr(internal_auth, "_INTERNAL_AUTH_TOKEN", "the-internal-token")
    client = TestClient(_make_app(), base_url="https://deerflow.example")

    response = client.post(
        "/api/threads/abc/runs/stream",
        headers={
            "Origin": "https://deerflow.example",
            internal_auth.INTERNAL_AUTH_HEADER_NAME: "wrong-token",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "CSRF token missing. Include X-CSRF-Token header."


# ---------------------------------------------------------------------------
# [argus patch #28] /webhooks/ exemptions (regression guard for the v2 rebase,
# where these were briefly lost — Telegram webhooks 403'd at the auth layer).
# ---------------------------------------------------------------------------


def test_webhooks_path_is_csrf_exempt():
    """Telegram pushes server-to-server to /webhooks/telegram with no browser
    cookie; CSRF must not apply (the route verifies the secret-token header)."""
    from types import SimpleNamespace

    from app.gateway.csrf_middleware import should_check_csrf

    req = SimpleNamespace(
        method="POST",
        url=SimpleNamespace(path="/webhooks/telegram"),
        headers={},
        cookies={},
    )
    assert should_check_csrf(req) is False


def test_webhooks_path_is_public_in_auth_middleware():
    """The auth middleware must treat /webhooks/ as public, else the request is
    401/403'd before it reaches the webhook route's own secret check."""
    from app.gateway.auth_middleware import _is_public

    assert _is_public("/webhooks/telegram") is True
    assert _is_public("/api/threads/abc/runs/stream") is False

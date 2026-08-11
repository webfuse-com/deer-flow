"""CSRF protection middleware for FastAPI.

Per RFC-001:
State-changing operations require CSRF protection.
"""

import os
import secrets
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from app.gateway.auth.config import get_auth_config
from app.gateway.auth.session_cookie_state import SESSION_COOKIE_ISSUED_STATE_ATTR, SESSION_COOKIE_MAX_AGE_STATE_ATTR, SESSION_COOKIE_SECURE_STATE_ATTR, SKIP_AUTH_CSRF_COOKIE_STATE_ATTR
from app.gateway.auth_disabled import is_auth_disabled
from app.gateway.request_path import get_request_route_path
from app.gateway.sso_auth import trusted_sso_email

CSRF_COOKIE_NAME = "csrf_token"
CSRF_HEADER_NAME = "X-CSRF-Token"
CSRF_TOKEN_LENGTH = 64  # bytes
_CSRF_STATE_CHANGING_METHODS: frozenset[str] = frozenset({"POST", "PUT", "DELETE", "PATCH"})
_CSRF_EXEMPT_EXACT_PATHS: frozenset[str] = frozenset({"/api/v1/auth/me"})


def is_secure_request(request: Request) -> bool:
    """Detect whether the original client request was made over HTTPS."""
    return _request_scheme(request) == "https"


def generate_csrf_token() -> str:
    """Generate a secure random CSRF token."""
    return secrets.token_urlsafe(CSRF_TOKEN_LENGTH)


def should_check_csrf(request: Request) -> bool:
    """Determine if a request needs CSRF validation.

    CSRF is checked for state-changing methods (POST, PUT, DELETE, PATCH).
    GET, HEAD, OPTIONS, and TRACE are exempt per RFC 7231.
    """
    if request.method not in _CSRF_STATE_CHANGING_METHODS:
        return False

    if is_auth_disabled():
        return False

    route_path = get_request_route_path(request)
    path = route_path.rstrip("/")
    # Exempt host-owned endpoints that implement their own request posture.
    if path in _CSRF_EXEMPT_EXACT_PATHS:
        return False
    # Inbound webhooks authenticate themselves via provider-specific signatures
    # (e.g. GitHub's X-Hub-Signature-256), not the CSRF double-submit cookie.
    if route_path.startswith("/api/webhooks/"):
        return False
    # [argus patch #28] Telegram webhook — server-to-server pushes from Telegram,
    # not browser cookies, so CSRF does not apply; verified by the
    # X-Telegram-Bot-Api-Secret-Token header in the route handler.
    if path.startswith("/webhooks/"):
        return False
    # [argus patch #50] Exempt the connector call proxy. Same-origin apps
    # carry the CSRF cookie; api tools calling from outside use the
    # internal token (caught above). Direct curl testing without either
    # is also exempt here because the route has no side effects beyond
    # calling a connector function (which is the product working).
    # Both prefixes: /api/transformers/ is the pre-2026-08-06 name, still
    # hard-coded in apps published before the rename.
    if path.startswith(("/api/connectors/", "/api/transformers/")):
        return False
    # [argus patch #51] Exempt the overlay tool call proxy. Same model as
    # the connector proxy: app-tier calls are cross-origin (credentials off),
    # so there is no CSRF cookie to double-submit. The tool's own safety
    # wrapper (e.g. chargebee's 4-operation limit) is the scope.
    if path.startswith("/api/apps/") and "/tools/" in path:
        return False
    # [argus patch #30] Exempt requests that carry a valid internal-auth token.
    # CSRF defends against a browser silently attaching ambient cookies on a
    # cross-site request; a trusted service call bearing the shared internal
    # secret is not that vector, and the token is a strictly stronger guard than
    # the double-submit cookie. This lets Chronos fire /api/playbooks/<id>/fire
    # (and any internal caller) without minting a dummy cookie pair. Non-internal
    # callers are unaffected — without the token this falls through to True.
    from app.gateway.internal_auth import INTERNAL_AUTH_HEADER_NAME, is_valid_internal_auth_token

    if is_valid_internal_auth_token(request.headers.get(INTERNAL_AUTH_HEADER_NAME)):
        return False
    return True


_AUTH_EXEMPT_PATHS: frozenset[str] = frozenset(
    {
        "/api/v1/auth/login/local",
        "/api/v1/auth/logout",
        "/api/v1/auth/register",
        "/api/v1/auth/initialize",
    }
)


def is_auth_endpoint(request: Request) -> bool:
    """Check if the request is to an auth endpoint.

    Auth endpoints don't need CSRF validation on first call (no token).
    """
    return get_request_route_path(request).rstrip("/") in _AUTH_EXEMPT_PATHS


def _host_with_optional_port(hostname: str, port: int | None, scheme: str) -> str:
    """Return normalized host[:port], omitting default ports."""
    host = hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"

    if port is None or (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        return host
    return f"{host}:{port}"


def _normalize_origin(origin: str) -> str | None:
    """Return a normalized scheme://host[:port] origin, or None for invalid input."""
    try:
        parsed = urlsplit(origin.strip())
        port = parsed.port
    except ValueError:
        return None

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        return None

    # Browser Origin is only scheme/host/port. Reject URL-shaped or credentialed values.
    if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        return None

    return f"{scheme}://{_host_with_optional_port(parsed.hostname, port, scheme)}"


def _configured_cors_origins() -> set[str]:
    """Return explicit configured browser origins that may call auth routes."""
    origins = set()
    for raw_origin in os.environ.get("GATEWAY_CORS_ORIGINS", "").split(","):
        origin = raw_origin.strip()
        if not origin or origin == "*":
            continue
        normalized = _normalize_origin(origin)
        if normalized:
            origins.add(normalized)
    return origins


def get_configured_cors_origins() -> set[str]:
    """Return normalized explicit browser origins from GATEWAY_CORS_ORIGINS."""
    return _configured_cors_origins()


# Response headers a split-origin browser client must be able to read. Only the
# CORS-safelisted set is visible to JS by default, and the created run's id
# travels in `Content-Location` — the LangGraph SDK resolves run metadata from
# it, so withholding it leaves such a client unable to learn its own run id.
CORS_EXPOSED_HEADERS: tuple[str, ...] = ("Content-Location",)


def _first_header_value(value: str | None) -> str | None:
    """Return the first value from a comma-separated proxy header."""
    if not value:
        return None
    first = value.split(",", 1)[0].strip()
    return first or None


def _forwarded_param(request: Request, name: str) -> str | None:
    """Extract a parameter from the first RFC 7239 Forwarded header entry."""
    forwarded = _first_header_value(request.headers.get("forwarded"))
    if not forwarded:
        return None

    for part in forwarded.split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key.lower() == name:
            return value.strip().strip('"') or None
    return None


def _request_scheme(request: Request) -> str:
    """Resolve the original request scheme from trusted proxy headers."""
    scheme = _forwarded_param(request, "proto") or _first_header_value(request.headers.get("x-forwarded-proto")) or request.url.scheme
    return scheme.lower()


def _request_origin(request: Request) -> str | None:
    """Build the origin for the URL the browser is targeting."""
    scheme = _request_scheme(request)
    host = _forwarded_param(request, "host") or _first_header_value(request.headers.get("x-forwarded-host")) or request.headers.get("host") or request.url.netloc

    forwarded_port = _first_header_value(request.headers.get("x-forwarded-port"))
    if forwarded_port and ":" not in host.rsplit("]", 1)[-1]:
        host = f"{host}:{forwarded_port}"

    return _normalize_origin(f"{scheme}://{host}")


def is_allowed_auth_origin(request: Request) -> bool:
    """Allow auth POSTs only from the same origin or explicit configured origins.

    Login/register/initialize are exempt from the double-submit token because
    first-time browser clients do not have a CSRF token yet. They still create
    a session cookie, so browser requests with a hostile Origin header must be
    rejected to prevent login CSRF / session fixation. Requests without Origin
    are allowed for non-browser clients such as curl and mobile integrations.
    """
    origin = request.headers.get("origin")
    if not origin:
        return True

    normalized_origin = _normalize_origin(origin)
    if normalized_origin is None:
        return False

    request_origin = _request_origin(request)
    return normalized_origin in _configured_cors_origins() or (request_origin is not None and normalized_origin == request_origin)


def auth_csrf_cookie_settings(request: Request) -> tuple[bool, int | None]:
    """Return ``(secure, max_age)`` for auth-created CSRF cookies."""
    session_cookie_issued = getattr(request.state, SESSION_COOKIE_ISSUED_STATE_ATTR, False)
    if session_cookie_issued:
        return (
            bool(getattr(request.state, SESSION_COOKIE_SECURE_STATE_ATTR, is_secure_request(request))),
            getattr(request.state, SESSION_COOKIE_MAX_AGE_STATE_ATTR, None),
        )

    secure = is_secure_request(request)
    max_age = get_auth_config().token_expiry_days * 24 * 3600 if secure else None
    return secure, max_age


class CSRFMiddleware(BaseHTTPMiddleware):
    """Middleware that implements CSRF protection using Double Submit Cookie pattern."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        _is_auth = is_auth_endpoint(request)

        # [argus patch #16] Trusted-proxy SSO citizens never POST the local login
        # endpoint, so the only place that mints the csrf_token cookie (the
        # `_is_auth and POST` branch below) never fires for them. The double-submit
        # check then 403s their first state-changing request with "CSRF token
        # missing" because there is no cookie to echo. Treat a trusted-SSO request
        # with no csrf_token cookie yet as first contact: skip the double-submit
        # rejection for THIS request and mint the cookie on the response, exactly
        # as a login POST would. The trusted_sso_email gate (proxy-secret,
        # constant-time) means a direct tailnet caller cannot use this to bypass
        # CSRF. Once the cookie is set, subsequent requests take the normal path.
        _sso_first_contact = trusted_sso_email(request.headers) is not None and not request.cookies.get(CSRF_COOKIE_NAME)

        if should_check_csrf(request) and _is_auth and not is_allowed_auth_origin(request):
            return JSONResponse(
                status_code=403,
                content={"detail": "Cross-site auth request denied."},
            )

        if should_check_csrf(request) and not _is_auth and not _sso_first_contact:
            cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
            header_token = request.headers.get(CSRF_HEADER_NAME)

            if not cookie_token or not header_token:
                return JSONResponse(
                    status_code=403,
                    content={"detail": "CSRF token missing. Include X-CSRF-Token header."},
                )

            if not secrets.compare_digest(cookie_token, header_token):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "CSRF token mismatch."},
                )

        response = await call_next(request)

        # Mint the CSRF cookie for sessions that establish here: auth-endpoint
        # POSTs (local login/register) and [argus patch #16] trusted-SSO first contact.
        if (_is_auth and request.method == "POST" and not getattr(request.state, SKIP_AUTH_CSRF_COOKIE_STATE_ATTR, False)) or _sso_first_contact:
            # Generate a new CSRF token for the session
            csrf_token = generate_csrf_token()
            secure, max_age = auth_csrf_cookie_settings(request)
            response.set_cookie(
                key=CSRF_COOKIE_NAME,
                value=csrf_token,
                httponly=False,  # Must be JS-readable for Double Submit Cookie pattern
                secure=secure,
                samesite="strict",
                # Match the access_token cookie's lifetime (auth.py::_set_session_cookie)
                # so the double-submit pair never diverges. A session-only csrf_token is
                # evicted when iOS Safari terminates a home-screen PWA while the persistent
                # access_token survives — leaving the user "logged in" but unable to make
                # any state-changing request (403 "CSRF token missing").
                max_age=max_age,
            )

        return response


def get_csrf_token(request: Request) -> str | None:
    """Get the CSRF token from the current request's cookies.

    This is useful for server-side rendering where you need to embed
    token in forms or headers.
    """
    return request.cookies.get(CSRF_COOKIE_NAME)

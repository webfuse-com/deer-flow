"""Global authentication middleware — fail-closed safety net.

Rejects unauthenticated requests to non-public paths with 401. When a
request passes the cookie check, resolves the JWT payload to a real
``User`` object and stamps it into both ``request.state.user`` and the
``deerflow.runtime.user_context`` contextvar so that repository-layer
owner filtering works automatically via the sentinel pattern.

Fine-grained permission checks remain in authz.py decorators.
"""

import logging
from collections.abc import Callable

from fastapi import HTTPException, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from app.gateway.auth.errors import AuthErrorCode, AuthErrorResponse
from app.gateway.auth_disabled import (
    AUTH_SOURCE_AUTH_DISABLED,
    AUTH_SOURCE_INTERNAL,
    AUTH_SOURCE_SESSION,
    get_auth_disabled_user,
    is_auth_disabled,
)
from app.gateway.authz import AuthContext, resolve_route_permissions
from app.gateway.internal_auth import INTERNAL_AUTH_HEADER_NAME, get_internal_user, is_valid_internal_auth_token
from app.gateway.request_path import get_request_route_path
from app.gateway.sso_auth import sso_email_allowed, trusted_sso_email
from deerflow.runtime.user_context import reset_current_user, set_current_user

logger = logging.getLogger(__name__)

# Paths that never require authentication.
_PUBLIC_PATH_PREFIXES: tuple[str, ...] = (
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/api/v1/auth/oauth/",
    "/api/v1/auth/callback/",
    # Inbound webhooks authenticate themselves via provider-specific signatures
    # (e.g. GitHub's X-Hub-Signature-256), not session cookies.
    "/api/webhooks/",
    # [argus patch #28] Telegram webhook — these are server-to-server pushes from
    # Telegram, not browser sessions; they carry no auth cookie and are verified
    # by the X-Telegram-Bot-Api-Secret-Token header in the route handler itself.
    "/webhooks/",
)

# Exact auth paths that are public (login/register/status check).
# /api/v1/auth/me, /api/v1/auth/change-password etc. are NOT public.
_PUBLIC_EXACT_PATHS: frozenset[str] = frozenset(
    {
        "/api/v1/auth/login/local",
        "/api/v1/auth/register",
        "/api/v1/auth/logout",
        "/api/v1/auth/setup-status",
        "/api/v1/auth/initialize",
        "/api/v1/auth/providers",
    }
)


def _is_public(path: str) -> bool:
    stripped = path.rstrip("/")
    if stripped in _PUBLIC_EXACT_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in _PUBLIC_PATH_PREFIXES)


class AuthMiddleware(BaseHTTPMiddleware):
    """Strict auth gate: reject requests without a valid session.

    Two-stage check for non-public paths:

    1. Cookie presence — return 401 NOT_AUTHENTICATED if missing
    2. JWT validation via ``get_optional_user_from_request`` — return 401
       TOKEN_INVALID if the token is absent, malformed, expired, or the
       signed user does not exist / is stale

    On success, stamps ``request.state.user`` and the
    ``deerflow.runtime.user_context`` contextvar so that repository-layer
    owner filters work downstream without every route needing a
    ``@require_auth`` decorator. Routes that need per-resource
    authorization (e.g. "user A cannot read user B's thread by guessing
    the URL") should additionally use ``@require_permission(...,
    owner_check=True)`` for explicit enforcement — but authentication
    itself is fully handled here.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if _is_public(get_request_route_path(request)):
            return await call_next(request)

        # [argus] CORS preflight. A browser sends OPTIONS with no cookies and
        # no auth header by specification, so gating it here 401s every
        # cross-origin call before the route is ever reached — the browser
        # then reports an opaque CORS failure rather than the real reason.
        # Safe to let through: OPTIONS changes no state, and the route's own
        # preflight handler returns Allow-Origin only for origins it
        # recognises, so an unknown origin still gets nothing usable.
        # Needed by the app tier (apps-<citizen>.acro.surfly.com), which is a
        # deliberately separate origin — see routers/transformers_proxy.py.
        if request.method == "OPTIONS":
            return await call_next(request)

        internal_user = None
        if is_valid_internal_auth_token(request.headers.get(INTERNAL_AUTH_HEADER_NAME)):
            # Extract the channel owner user ID from the trusted header.
            # When present, the synthetic internal user carries the actual
            # owner identity so that get_effective_user_id() and per-user
            # filesystem paths (custom skills, memory, thread data) resolve
            # to the IM channel user instead of falling back to "default".
            from app.gateway.internal_auth import INTERNAL_OWNER_USER_ID_HEADER_NAME

            owner_user_id = request.headers.get(INTERNAL_OWNER_USER_ID_HEADER_NAME)
            if owner_user_id:
                owner_user_id = owner_user_id.strip()
            internal_user = get_internal_user(owner_user_id=owner_user_id or None)

        auth_source = AUTH_SOURCE_SESSION
        access_token = request.cookies.get("access_token")

        # [argus patch #15] Trusted-proxy SSO: when Caddy has authenticated the
        # browser via Google SSO it injects a verified X-Auth-Email plus the
        # proxy secret. If both are present (and there is no internal token and
        # no cookie session), resolve/auto-provision the user by email so the
        # citizen is not asked to log in a second time. The secret gate
        # (sso_auth) blocks a direct tailnet caller from forging the email.
        sso_user = None
        if internal_user is None and not access_token:
            _sso_email = trusted_sso_email(request.headers)
            if _sso_email:
                # [argus patch #55] Authenticated is not authorized. On a
                # single-citizen atlas-<name> stack the run executes AS the
                # owner, so provisioning an account for a different citizen
                # hands them the owner's credentials, knowledge ring and
                # read-write mounts. Refuse BEFORE resolve_or_provision, so a
                # rejected visitor leaves no user row behind either.
                if not sso_email_allowed(_sso_email, request.url.path):
                    logger.warning(
                        "SSO identity %s rejected on %s (stack owner mismatch)",
                        _sso_email,
                        request.url.path,
                    )
                    # 403, not 401: the SPA keys its login redirect off the
                    # HTTP status (core/api/fetcher.ts, AuthProvider.tsx), so a
                    # 401 here would bounce a rejected citizen through Google
                    # and straight back into the same refusal, forever. The
                    # code string stays NOT_AUTHENTICATED because it is the
                    # closest member of the frontend's AUTH_ERROR_CODES union
                    # (core/auth/types.ts); the reason is carried in `message`.
                    return JSONResponse(
                        status_code=403,
                        content={
                            "detail": AuthErrorResponse(
                                code=AuthErrorCode.NOT_AUTHENTICATED,
                                message=("This Atlas stack belongs to another citizen. Each stack runs as its owner, so it cannot be used on their behalf."),
                            ).model_dump()
                        },
                    )

                from app.gateway.deps import resolve_or_provision_sso_user

                sso_user = await resolve_or_provision_sso_user(_sso_email)

        # Non-public path: require session cookie (or internal token, or SSO)
        if internal_user is not None:
            user = internal_user
            auth_source = AUTH_SOURCE_INTERNAL
        elif sso_user is not None:
            user = sso_user
        elif access_token:
            # Strict JWT validation: reject junk/expired tokens with 401
            # right here instead of silently passing through. This closes
            # the "junk cookie bypass" gap (AUTH_TEST_PLAN test 7.5.8):
            # without this, non-isolation routes like /api/models would
            # accept any cookie-shaped string as authentication.
            #
            # We call the *strict* resolver so that fine-grained error
            # codes (token_expired, token_invalid, user_not_found, …)
            # propagate from AuthErrorCode, not get flattened into one
            # generic code. BaseHTTPMiddleware doesn't let HTTPException
            # bubble up, so we catch and render it as JSONResponse here.
            from app.gateway.deps import get_current_user_from_request

            try:
                user = await get_current_user_from_request(request)
            except HTTPException as exc:
                if not is_auth_disabled():
                    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
                user = get_auth_disabled_user()
                auth_source = AUTH_SOURCE_AUTH_DISABLED
        elif is_auth_disabled():
            user = get_auth_disabled_user()
            auth_source = AUTH_SOURCE_AUTH_DISABLED
        else:
            return JSONResponse(
                status_code=401,
                content={
                    "detail": AuthErrorResponse(
                        code=AuthErrorCode.NOT_AUTHENTICATED,
                        message="Authentication required",
                    ).model_dump()
                },
            )

        # Stamp both request.state.user (for the contextvar pattern)
        # and request.state.auth (so @require_permission's "auth is
        # None" branch short-circuits instead of running the entire
        # JWT-decode + DB-lookup pipeline a second time per request).
        request.state.user = user
        request.state.auth_source = auth_source
        permissions = await resolve_route_permissions(
            user,
            is_internal=auth_source == AUTH_SOURCE_INTERNAL,
        )
        request.state.auth = AuthContext(user=user, permissions=permissions)
        token = set_current_user(user)
        try:
            return await call_next(request)
        finally:
            reset_current_user(token)

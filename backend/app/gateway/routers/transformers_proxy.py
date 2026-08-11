"""Gateway router for transformer function calls from apps.

[argus patch #50] Apps deployed at /app/<slug>/ on a stack's gateway are
same-origin with /api/*, so JavaScript in an app can fetch a transformer
function via this route. The gateway proxies to Chronos's call surface
(server-to-server on argus-net).

Multi-citizen: when citizen B opens citizen A's app and the JS calls this
route, it hits citizen A's gateway. The transformer runs on citizen A's
stack with A's credentials. The caller's SSO email is logged in the run
row for audit. This is the same model as /api/playbooks/{id}/fire: the
app owner's credentials power the integration; the visitor triggers it.

No X-Transformer-Key header needed from JS. The gateway authenticates to
Chronos with SCHEDULER_API_KEY (the same key every gateway already holds
for its diag tools and schedule reads).

Route: POST /api/connectors/{name}/fn/{function}
Body: JSON (the function inputs)
Returns: {"run_id": N, "result": ...} or {"run_id": N, "error": "..."}
"""

from __future__ import annotations

import logging
import os
import re

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/connectors", tags=["connectors"])

# Deprecated alias. Apps published before the 2026-08-06 rename hard-code
# /api/transformers/<name>/fn/<function> in their JavaScript and are static
# files on disk — they will not update themselves. Both prefixes stay served
# until those apps are rewritten. Registered after the handler below.
legacy_router = APIRouter(prefix="/api/transformers", tags=["connectors"], deprecated=True, include_in_schema=False)

CHRONOS_URL = os.environ.get("CHRONOS_URL", "http://argus-scheduler:8000")
SCHEDULER_API_KEY = os.environ.get("SCHEDULER_API_KEY", "")


# The app tier runs on its own origin (apps-<citizen>.acro.surfly.com) so that
# an agent-authored page never carries the citizen's session — see
# plans/2026-08-06-app-origin-isolation.md. That makes connector calls
# cross-origin, so this route needs its own CORS handling.
#
# Deliberately NOT added to GATEWAY_CORS_ORIGINS: that middleware sets
# allow_credentials=True, which would hand the session straight back to the app
# and undo the isolation. Here credentials stay OFF. Identity still reaches the
# connector — Caddy asserts X-Auth-Email server-side and it comes back as
# `called_by` — so a page can know its viewer without holding a session.
_APP_ORIGIN_RE = re.compile(r"^https://apps-[a-z0-9-]+\.acro\.surfly\.com$")


def _app_origin(request: Request) -> str | None:
    """The request Origin if it is an app-tier origin, else None."""
    origin = request.headers.get("origin", "")
    return origin if origin and _APP_ORIGIN_RE.match(origin) else None


def _cors_headers(origin: str) -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": origin,
        # NO Access-Control-Allow-Credentials. Omitting it is the boundary.
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Vary": "Origin",
        "Access-Control-Max-Age": "600",
    }


def _stack() -> str:
    """The stack this gateway runs as."""
    uid = os.environ.get("PERSONAL_USER_ID", "")
    return f"atlas-{uid}" if uid else os.environ.get("ATLAS_STACK", "")


class CallRequest(BaseModel):
    # The full JSON body IS the inputs dict (no wrapper).
    # FastAPI passes it through as a dict since we don't define fields.
    pass


@router.options("/{name}/fn/{function}", include_in_schema=False)
async def preflight_connector(name: str, function: str, request: Request) -> Response:
    """CORS preflight for app-tier callers. Unknown origins get no headers,
    so the browser refuses the call."""
    origin = _app_origin(request)
    return Response(status_code=204, headers=_cors_headers(origin) if origin else {})


@router.post("/{name}/fn/{function}")
async def call_connector(name: str, function: str, request: Request, response: Response):
    """Call a connector function. Reachable from the app tier cross-origin
    (credentials off) and from this stack same-origin. Proxies to Chronos."""
    origin = _app_origin(request)
    cors = _cors_headers(origin) if origin else {}
    if cors:
        response.headers.update(cors)

    def fail(status: int, detail: str) -> HTTPException:
        """HTTPException carrying the CORS headers. Without them the browser
        reports an opaque CORS error and the citizen never sees the real
        status — FastAPI's exception path does not use the injected Response."""
        return HTTPException(status, detail=detail, headers=cors or None)

    stack = _stack()
    if not stack:
        raise fail(503, "stack not configured on this gateway")

    if not SCHEDULER_API_KEY:
        raise fail(503, "SCHEDULER_API_KEY not configured")

    body = await request.body()

    # Capture the caller's email for audit (from SSO, forwarded by Caddy)
    caller_email = request.headers.get("X-Auth-Email", "")

    url = f"{CHRONOS_URL}/api/connectors/{stack}/{name}/fn/{function}"
    # Send the internal token so Chronos recognizes this as a trusted
    # server-to-server call. The gateway is authenticated via SSO
    # (same-origin apps) or the internal token (api tools).
    internal_token = os.environ.get("DEER_FLOW_INTERNAL_AUTH_TOKEN", "")
    headers = {
        "X-Scheduler-Api-Key": SCHEDULER_API_KEY,
        "X-DeerFlow-Internal-Token": internal_token,
    }

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(url, headers=headers, content=body)
    except httpx.HTTPError as e:
        logger.warning("transformer call proxy failed: %s", e)
        raise fail(502, f"chronos unreachable: {e}")

    if resp.status_code == 404:
        raise fail(404, resp.json().get("detail", "not found"))
    if resp.status_code == 401:
        raise fail(403, "connector requires a call key (not needed from app-tier or same-origin callers; check SCHEDULER_API_KEY)")
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text[:200])
        except Exception:
            detail = resp.text[:200]
        raise fail(resp.status_code, detail)

    result = resp.json()

    # Add caller audit if we know who it was
    if caller_email and isinstance(result, dict):
        result["called_by"] = caller_email

    return result


# Same handlers, old path. One implementation, two routes — see the note above.
legacy_router.add_api_route(
    "/{name}/fn/{function}",
    call_connector,
    methods=["POST"],
    include_in_schema=False,
)
legacy_router.add_api_route(
    "/{name}/fn/{function}",
    preflight_connector,
    methods=["OPTIONS"],
    include_in_schema=False,
)

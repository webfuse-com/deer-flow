"""Gateway router for calling overlay tools from app frontends.

[argus patch #51] Exposes allowlisted overlay tools via HTTP so Atlas app
frontends can call them via fetch(). The overlay tools run in-process in
the gateway, so credentials stay in the gateway's env (site-wide .env +
per-stack secrets) and are never exposed to citizens or app pages.

Two-layer scoping prevents unauthorized tool access from apps:

1. Infra allowlist (http-exposed-tools.json, operator-controlled, ships in
   the image): decides which overlay tools are HTTP-callable at all. A tool
   not in this file cannot be reached via HTTP, period.

2. Per-app declaration (app.json "http_tools"): each app must declare which
   tools it needs. The gateway reads app.json from the apps/ mount. An app
   that doesn't declare a tool gets 403, even if the tool is in the infra
   allowlist.

A Referer header check prevents app A from calling app B's declared tools
by guessing the URL: the Referer must match the app_slug in the path. If
Referer is absent (browser stripped it), the app.json declaration remains
the gate.

Route: POST /api/apps/{app_slug}/tools/{tool_name}
Body: JSON (the tool's input arguments as a flat dict)
Returns: {"signup_link": ..., "called_by": ...} or {"error": "...", "called_by": ...}

Same CORS model as transformers_proxy.py: credentials OFF, app origin only.
Identity reaches the tool via X-Auth-Email (asserted by Caddy SSO), returned
as called_by.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/apps", tags=["tools"])

# Same CORS pattern as transformers_proxy.py — app origin only, no credentials.
_APP_ORIGIN_RE = re.compile(r"^https://apps-[a-z0-9-]+\.acro\.surfly\.com$")

# Apps mount path (RO, set in every gateway Quadlet).
_APPS_DIR = Path(os.environ.get("ARGUS_APPS_DIR", "/app/repo/apps"))

# Infra allowlist, shipped in the image at site-packages alongside overlay modules.
_SITE_PACKAGES = Path("/app/backend/.venv/lib/python3.12/site-packages")
_ALLOWLIST_PATH = _SITE_PACKAGES / "http-exposed-tools.json"


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


def _load_allowlist() -> dict[str, Any]:
    """Load the infra allowlist. Returns {tool_name: {module, attribute, ...}}."""
    try:
        data = json.loads(_ALLOWLIST_PATH.read_text())
        return data.get("tools", {})
    except (OSError, json.JSONDecodeError) as e:
        logger.error("failed to load http-exposed-tools.json: %s", e)
        return {}


def _load_app_http_tools(app_slug: str) -> list[str]:
    """Read the http_tools list from the app's app.json."""
    app_json_path = _APPS_DIR / app_slug / "app.json"
    if not app_json_path.exists():
        return []
    try:
        data = json.loads(app_json_path.read_text())
        return data.get("http_tools", [])
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("failed to read app.json for %s: %s", app_slug, e)
        return []


def _verify_referer(request: Request, app_slug: str) -> bool:
    """Check the Referer header matches the calling app.

    Returns True if Referer is absent (the app.json gate is the primary
    defense) or if it matches the app_slug in the path. Returns False only
    when a Referer IS present and points to a DIFFERENT app.
    """
    referer = request.headers.get("referer", "")
    if not referer:
        return True
    m = re.match(r"^https://apps-[a-z0-9-]+\.acro\.surfly\.com/([^/]+)", referer)
    if not m:
        return True
    return m.group(1) == app_slug


@router.options("/{app_slug}/tools/{tool_name}", include_in_schema=False)
async def preflight_tool(app_slug: str, tool_name: str, request: Request) -> Response:
    """CORS preflight for app-tier callers."""
    origin = _app_origin(request)
    return Response(status_code=204, headers=_cors_headers(origin) if origin else {})


@router.post("/{app_slug}/tools/{tool_name}")
async def call_overlay_tool(
    app_slug: str, tool_name: str, request: Request, response: Response
):
    """Call an overlay tool from an app frontend.

    Two-layer scoping: the infra allowlist decides which tools are
    HTTP-callable; the app's app.json declares which tools it needs.
    """
    origin = _app_origin(request)
    cors = _cors_headers(origin) if origin else {}
    if cors:
        response.headers.update(cors)

    def fail(status: int, detail: str) -> HTTPException:
        return HTTPException(status, detail=detail, headers=cors or None)

    # 1. Referer check: prevent cross-app calls.
    if not _verify_referer(request, app_slug):
        raise fail(403, "request does not originate from this app")

    # 2. Infra allowlist: is this tool HTTP-callable at all?
    allowlist = _load_allowlist()
    entry = allowlist.get(tool_name)
    if entry is None:
        raise fail(404, f"tool '{tool_name}' is not HTTP-callable")

    # 3. Per-app declaration: did this app declare the tool?
    app_tools = _load_app_http_tools(app_slug)
    if tool_name not in app_tools:
        raise fail(403, f"tool '{tool_name}' is not declared by app '{app_slug}'")

    # 4. Import the overlay module and resolve the tool.
    module_name = entry.get("module", "")
    attr_name = entry.get("attribute", "")
    if not module_name or not attr_name:
        raise fail(500, f"allowlist entry for '{tool_name}' is missing module/attribute")

    try:
        mod = __import__(module_name)
        tool_fn = getattr(mod, attr_name)
    except (ImportError, AttributeError) as e:
        logger.error("failed to load overlay tool %s.%s: %s", module_name, attr_name, e)
        raise fail(500, f"tool '{tool_name}' could not be loaded")

    # 5. Call the tool in-process.
    body = await request.json()
    caller_email = request.headers.get("X-Auth-Email", "")

    try:
        result = tool_fn.invoke(body)
    except Exception as e:
        logger.exception("overlay tool '%s' raised", tool_name)
        return {"error": str(e), "called_by": caller_email}

    # Overlay tools return JSON strings; parse for a clean response.
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
            if isinstance(parsed, dict):
                parsed["called_by"] = caller_email
                return parsed
            return {"result": parsed, "called_by": caller_email}
        except json.JSONDecodeError:
            return {"result": result, "called_by": caller_email}

    return {"result": result, "called_by": caller_email}

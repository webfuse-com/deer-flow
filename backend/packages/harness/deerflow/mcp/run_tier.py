"""[argus patch #174, #175] ``X-Argus-Run-Tier`` on every MCP call, set last.

The interceptor is built in (``mcp/interceptors.py`` appends it after the
citizen-configured ``mcpInterceptors``), so it runs closest to the transport:
no configured interceptor can remove or rewrite the header after it, and a
static header of any spelling in ``extensions_config.json`` is replaced
(``apply_header_overrides`` with the server's own spellings).

Per call:

1. The run's tier comes from the live run context (``request.runtime``, then
   ``get_runtime()``, as ``context_headers.py`` reads it); a call outside a
   run, or a run without a tier, is sent as ``owner``.
2. A restricted tier needs a policy: none means the call is refused. With one,
   a tier with a ceiling (public) is refused unless the server is a deployment
   server whose URL is on the policy's list (``tier_policy.mcp_server_allowed``);
   the owner tier (#175) may call every server.
3. HTTP/SSE servers get ``X-Argus-Run-Tier: <tier>``. atlas-mcp refuses every
   suite for a restricted value and kb-api clamps to the internal ring.
4. A restricted run's call drops any ``X-Pythia-Caller-Token`` an earlier
   interceptor added (the overlay's ``argus_caller_token`` mints one from the
   stack email, not from the run context), so no personal-ring token leaves
   a restricted run even before the overlay and kb-api learn the header. The
   owner tier drops it only on the policy's listed servers (Pythia), which
   caps Pythia at the internal ring and keeps the owner's identity elsewhere.

Registered only when the image ships the policy file
(``tier_policy.last_known_tier_policy_file_present``): without it no run can
be tiered (admission and assembly refuse), and an absent header already means
``owner`` to every receiver.

No filesystem access on the event loop: an owner or dm call reads only the run
context, and a restricted call checks the policy and the deployment server list
in a worker thread (``asyncio.to_thread``), where the policy file's (path,
mtime, size) cache applies as everywhere else.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from langchain_core.tools import ToolException

from deerflow.agents.tier_policy import (
    ARGUS_TIER_KEY,
    RUN_TIER_HEADER,
    header_tier_value,
    is_restricted_tier,
    last_known_tier_policy_file_present,
    restricted_mcp_call_decision,
)
from deerflow.config.extensions_config import ExtensionsConfig
from deerflow.mcp.headers import apply_header_overrides, header_spellings

logger = logging.getLogger(__name__)

# Per-call header an earlier interceptor may add; never sent from a restricted run.
PYTHIA_CALLER_TOKEN_HEADER = "x-pythia-caller-token"


def _run_tier(request: Any) -> str | None:
    runtime = getattr(request, "runtime", None)
    if runtime is None:
        try:
            from langgraph.runtime import get_runtime

            runtime = get_runtime()
        except Exception:
            runtime = None
    context = getattr(runtime, "context", None)
    tier = context.get(ARGUS_TIER_KEY) if isinstance(context, dict) else None
    return tier if isinstance(tier, str) and tier else None


def build_run_tier_interceptor(extensions_config: ExtensionsConfig) -> Any | None:
    """The run-tier interceptor, or None when the image has no tier policy."""
    if not last_known_tier_policy_file_present():
        return None
    http_spellings: dict[str, dict[str, str]] = {name: header_spellings(server.headers) for name, server in extensions_config.mcp_servers.items() if server.type in ("http", "sse")}

    async def run_tier_interceptor(request: Any, handler: Any) -> Any:
        tier = _run_tier(request)
        restricted = is_restricted_tier(tier)
        withhold_token = False
        if restricted:
            refusal, withhold_token = await asyncio.to_thread(restricted_mcp_call_decision, tier, request.server_name)
            if refusal is not None:
                logger.warning("[tier-policy] refused MCP call to %r: %s", request.server_name, refusal)
                raise ToolException(f"MCP server '{request.server_name}' is not available in this conversation.")
        spellings = http_spellings.get(request.server_name)
        if spellings is None:
            return await handler(request)
        headers = apply_header_overrides(request.headers, {RUN_TIER_HEADER: header_tier_value(tier)}, spellings=spellings)
        if withhold_token:
            headers = {name: value for name, value in headers.items() if name.lower() != PYTHIA_CALLER_TOKEN_HEADER}
        return await handler(request.override(headers=headers))

    return run_tier_interceptor

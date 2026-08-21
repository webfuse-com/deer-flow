"""[argus patch #45] Delivery-outcome callback for scheduled playbook fires.

Chronos fires a playbook with an optional ``report_url``
(``{CHRONOS_INTERNAL_URL}/api/runs/{run_id}/output``) and closes the run row
only when the gateway reports how the unattended turn actually ended. Until
this patch the gateway never called back, so every ``channel_notify`` run sat
in ``running`` for CHRONOS_DELIVERY_REPORT_TIMEOUT_SEC (default 1800s) and was
closed as ``ok/unreported`` — the dashboard could not distinguish a delivered
briefing from a suppressed-silent tick or a failed turn.

Contract (mirrors Chronos ``scheduler/src/deerflow_client.py::fire_playbook``
and ``scheduler/src/routes/api.py::report_run_output``):

    POST {report_url}
    Header: X-DeerFlow-Internal-Token (same global secret Chronos used to fire)
    Body: {"status": "delivered"|"silent"|"failed",
           "channel": str?, "chat_id": str?, "message_text": str?,
           "delivered_at": ISO-8601 str?, "error": str?}

One POST, one retry on any failure, then give up with a log line — a report
failure must never fail (or delay-loop) the delivery itself. Fire-and-forget
callers should still ``await``: the timeout budget is small and awaiting keeps
ordering deterministic for tests and log correlation.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Literal

import httpx

logger = logging.getLogger(__name__)

# Chronos is one podman-network hop away; anything slower than this is down.
_REPORT_TIMEOUT_SECONDS = 10.0
_RETRY_DELAY_SECONDS = 2.0
# Defensive cap: message_text is stored in a JSONB column for the dashboard;
# the run detail page shows the full text but nobody needs a megabyte of it.
_MESSAGE_TEXT_CAP = 8192

DeliveryStatus = Literal["delivered", "silent", "failed"]


async def report_delivery(
    report_url: str | None,
    *,
    status: DeliveryStatus,
    channel: str | None = None,
    chat_id: str | None = None,
    message_text: str | None = None,
    error: str | None = None,
) -> bool:
    """POST the delivery outcome to *report_url*. Returns True when accepted.

    No-op (False) when *report_url* is empty. Never raises: one retry after a
    short delay, then a warning log — Chronos's poller closes the run as
    ``ok/unreported`` on its own timeout, exactly the pre-#45 behavior.
    """
    if not report_url:
        return False

    from app.gateway.internal_auth import create_internal_auth_headers

    if message_text and len(message_text) > _MESSAGE_TEXT_CAP:
        message_text = message_text[:_MESSAGE_TEXT_CAP] + " …[truncated]"

    payload: dict[str, str] = {"status": status}
    if channel:
        payload["channel"] = channel
    if chat_id:
        payload["chat_id"] = chat_id
    if message_text:
        payload["message_text"] = message_text
    if error:
        payload["error"] = error
    if status == "delivered":
        payload["delivered_at"] = datetime.now(UTC).isoformat()

    headers = create_internal_auth_headers()
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=_REPORT_TIMEOUT_SECONDS) as client:
                resp = await client.post(report_url, json=payload, headers=headers)
                resp.raise_for_status()
            logger.info("[DeliveryReport] reported %s to %s (attempt %d)", status, report_url, attempt)
            return True
        except Exception as exc:  # noqa: BLE001 — a report failure must never propagate
            if attempt == 1:
                logger.warning("[DeliveryReport] report to %s failed (attempt 1, retrying): %r", report_url, exc)
                await asyncio.sleep(_RETRY_DELAY_SECONDS)
            else:
                logger.warning(
                    "[DeliveryReport] report to %s failed (attempt 2, giving up; Chronos will close the run as unreported): %r",
                    report_url,
                    exc,
                )
    return False

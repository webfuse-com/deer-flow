"""Gateway router for IM channel management."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.gateway.deps import require_admin_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/channels", tags=["channels"])

_ADMIN_REQUIRED_DETAIL = "Admin privileges required to manage channel runtime workers."


class ChannelStatusResponse(BaseModel):
    service_running: bool
    channels: dict[str, dict]


class ChannelRestartResponse(BaseModel):
    success: bool
    message: str


@router.get("/", response_model=ChannelStatusResponse)
async def get_channels_status() -> ChannelStatusResponse:
    """Get the status of all IM channels."""
    from app.channels.service import get_channel_service

    service = get_channel_service()
    if service is None:
        return ChannelStatusResponse(service_running=False, channels={})
    status = service.get_status()
    return ChannelStatusResponse(**status)


@router.post("/{name}/restart", response_model=ChannelRestartResponse)
async def restart_channel(name: str, request: Request) -> ChannelRestartResponse:
    """Restart a specific IM channel."""
    await require_admin_user(request, detail=_ADMIN_REQUIRED_DETAIL)

    from app.channels.service import get_channel_service

    service = get_channel_service()
    if service is None:
        raise HTTPException(status_code=503, detail="Channel service is not running")

    success = await service.restart_channel(name)
    if success:
        logger.info("Channel %s restarted successfully", name)
        return ChannelRestartResponse(success=True, message=f"Channel {name} restarted successfully")
    else:
        logger.warning("Failed to restart channel %s", name)
        return ChannelRestartResponse(success=False, message=f"Failed to restart channel {name}")


# ---------------------------------------------------------------------------
# [argus patch #14] Proactive notify — scheduled turns ride the channel
# pipeline as synthetic inbound messages.
# ---------------------------------------------------------------------------


class ChannelNotifyRequest(BaseModel):
    chat_id: str
    text: str
    user_id: str | None = None
    topic_id: str | None = None


class ChannelNotifyResponse(BaseModel):
    accepted: bool
    channel: str
    chat_id: str


@router.post("/{name}/notify", response_model=ChannelNotifyResponse)
async def notify_channel(name: str, body: ChannelNotifyRequest, request: Request) -> ChannelNotifyResponse:
    """Inject a synthetic inbound message into a running channel.

    The message takes the exact path of a real user message: the manager
    maps (channel, chat_id, topic_id) to its DeerFlow thread, runs the
    agent turn, and the channel delivers the answer back to the chat —
    formatting, artifact delivery, and reply threading all reused.

    Guarded by the internal service token specifically (an SSO session is
    NOT sufficient): the caller chooses chat_id/user_id, so any weaker
    guard would allow injecting turns into another user's conversation.
    """
    from app.gateway.internal_auth import INTERNAL_AUTH_HEADER_NAME, is_valid_internal_auth_token

    if not is_valid_internal_auth_token(request.headers.get(INTERNAL_AUTH_HEADER_NAME)):
        raise HTTPException(status_code=403, detail="Internal token required")

    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="text must be non-empty")

    from app.channels.service import get_channel_service

    service = get_channel_service()
    if service is None:
        raise HTTPException(status_code=503, detail="Channel service is not running")
    if service.get_channel(name) is None:
        raise HTTPException(status_code=404, detail=f"Channel {name} is not running")

    from app.channels.message_bus import InboundMessage

    msg = InboundMessage(
        channel_name=name,
        chat_id=body.chat_id,
        user_id=body.user_id or body.chat_id,
        text=text,
        topic_id=body.topic_id,
    )
    await service.bus.publish_inbound(msg)
    logger.info("[Channels] notify accepted for %s chat_id=%s", name, body.chat_id)
    return ChannelNotifyResponse(accepted=True, channel=name, chat_id=body.chat_id)

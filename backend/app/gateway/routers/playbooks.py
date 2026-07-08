"""Gateway router for scheduled-playbook delivery.

[argus patch #30] A scheduled Atlas playbook (``schedules/<id>.md`` in the
citizen's fork, reconciled into a Chronos ``channel_notify`` job) fires here.
Chronos POSTs ``/api/playbooks/<schedule_id>/fire`` with the per-job metadata;
this endpoint sources the assembled prompt + the citizen's chat itself and
publishes a synthetic InboundMessage onto the channel bus. The whole channel
pipeline (thread mapping, the agent turn, Telegram formatting, artifact
delivery) is reused, exactly like ``/api/channels/{name}/notify`` (patch #14).

Auth bridge (the crux): ``DEER_FLOW_INTERNAL_AUTH_TOKEN`` is a single GLOBAL
secret in ``/opt/argus/.env`` — every per-stack gateway AND the argus-scheduler
(Chronos) container already hold the same value. So Chronos attaches it as the
internal token and this endpoint verifies it with ``is_valid_internal_auth_token``
— the same contract ``/notify`` enforces. No new secret, no per-stack token
distribution. The CSRF middleware additionally exempts any request carrying a
valid internal token (see csrf_middleware.py), so internal callers don't need to
mint a double-submit cookie pair.

Why the gateway sources prompt + chat (not Chronos): the prompt lives in a
per-stack mount the gateway already reads, and the chat id lives in the
gateway's own channel store. Keeping both here means Chronos stays a pure
trigger that holds no per-stack state (only the global internal token).
"""

from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/playbooks", tags=["playbooks"])

# The assembled playbook prompts are written by atlas-schedule-reconcile.sh to
# the per-stack ``config/atlas-playbooks/`` dir, mounted into the gateway at
# ``/app/repo/config``. Overridable for tests / alternate layouts.
PLAYBOOK_DIR = Path(os.environ.get("ATLAS_PLAYBOOK_DIR", "/app/repo/config/atlas-playbooks"))

# Memory policy values a job may declare (mirrors the schedules/*.md `memory:`
# frontmatter enum). Default is read-only for unattended job turns (§4b).
_VALID_MEMORY_MODES = {"off", "read-only", "read-write"}
_DEFAULT_MEMORY_MODE = "read-only"
# A job with no `agent:` runs as the citizen's personal agent (§3a).
_DEFAULT_AGENT = "atlas"


class PlaybookFireRequest(BaseModel):
    """Chronos forwards the job metadata. Only ``schedule_id`` is required; the
    rest default to the §3a/§4b runtime defaults when a job omits them.
    """

    schedule_id: str | None = None
    channel: str = "telegram"
    agent: str | None = None
    # `thread: own` reconciles to a synthetic topic_id "sched:<id>"; `shared`
    # (the default) leaves it None so the job runs in the citizen's one chat
    # thread (§3b).
    topic_id: str | None = None
    memory: str | None = None
    # [argus patch #43] Per-run tool whitelist declared in the schedule
    # frontmatter. Merged with skill allowed-tools by the tool policy filter.
    allowed_tools: list[str] | None = None


class PlaybookFireResponse(BaseModel):
    accepted: bool
    schedule_id: str
    channel: str
    agent: str
    memory: str
    chats: int


def _expand_dates(prompt: str) -> str:
    """Substitute date placeholders with concrete dates computed at fire time.

    Playbooks must not ask the local model to compute "today" or "this Monday"
    (it has no reliable clock and guesses). Resolve them here, where Python has
    the real wall clock. Mirrors scripts/atlas-briefing.py (now retired).

      {{TODAY}}        -> 2026-06-29   (ISO date the playbook fires)
      {{THIS_MONDAY}}  -> 2026-06-29   (Monday of the current week; == TODAY on Mondays)
    """
    today = date.today()
    this_monday = today - timedelta(days=today.weekday())
    return prompt.replace("{{TODAY}}", today.isoformat()).replace("{{THIS_MONDAY}}", this_monday.isoformat())


@router.post("/{schedule_id}/fire", response_model=PlaybookFireResponse)
async def fire_playbook(schedule_id: str, body: PlaybookFireRequest, request: Request) -> PlaybookFireResponse:
    """Fire a scheduled playbook and deliver it to the citizen's channel.

    Reads ``config/atlas-playbooks/<schedule_id>.md``, expands date
    placeholders, discovers the citizen's chat from the channel store, and
    publishes a synthetic InboundMessage (carrying the per-job agent / memory
    policy) onto the bus. Fire-and-forget: the channel pipeline runs the agent
    turn async and delivers the answer.
    """
    from app.gateway.internal_auth import INTERNAL_AUTH_HEADER_NAME, is_valid_internal_auth_token

    if not is_valid_internal_auth_token(request.headers.get(INTERNAL_AUTH_HEADER_NAME)):
        raise HTTPException(status_code=403, detail="Internal token required")

    # The path param is authoritative; the body field (if Chronos sends it) must
    # agree — guard against a mismatched fire.
    if body.schedule_id and body.schedule_id != schedule_id:
        raise HTTPException(status_code=400, detail="schedule_id path/body mismatch")

    # ---- source the assembled prompt -------------------------------------
    prompt_path = PLAYBOOK_DIR / f"{schedule_id}.md"
    try:
        prompt = prompt_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"playbook {schedule_id} not found at {prompt_path}")
    if not prompt:
        raise HTTPException(status_code=422, detail=f"playbook {schedule_id} prompt is empty")
    prompt = _expand_dates(prompt)

    # ---- resolve per-job agent / memory ----------------------------------
    agent = (body.agent or "").strip() or _DEFAULT_AGENT
    memory_mode = (body.memory or "").strip() or _DEFAULT_MEMORY_MODE
    if memory_mode not in _VALID_MEMORY_MODES:
        raise HTTPException(status_code=422, detail=f"invalid memory mode {memory_mode!r}; expected one of {sorted(_VALID_MEMORY_MODES)}")

    channel = (body.channel or "telegram").strip()

    # ---- discover the citizen's chat from the running channel store -------
    from app.channels.service import get_channel_service

    service = get_channel_service()
    if service is None:
        raise HTTPException(status_code=503, detail="Channel service is not running")
    if service.get_channel(channel) is None:
        raise HTTPException(status_code=404, detail=f"Channel {channel} is not running")

    chats = service.store.list_entries(channel)
    if not chats:
        # The citizen must have messaged the bot once so a thread mapping exists.
        raise HTTPException(
            status_code=409,
            detail=f"no {channel} chat mapped for this stack; the citizen must message the bot once first",
        )

    from app.channels.message_bus import InboundMessage

    delivered = 0
    for entry in chats:
        chat_id = entry.get("chat_id")
        if not chat_id:
            continue
        msg = InboundMessage(
            channel_name=channel,
            chat_id=str(chat_id),
            user_id=str(entry.get("user_id") or chat_id),
            text=prompt,
            topic_id=body.topic_id,
            agent_name=agent,
            unattended=True,
            memory_mode=memory_mode,
            allowed_tools=body.allowed_tools,
            metadata={"source": "playbook", "schedule_id": schedule_id},
        )
        await service.bus.publish_inbound(msg)
        delivered += 1

    logger.info(
        "[Playbooks] fired %s on %s for %d chat(s) (agent=%s memory=%s topic_id=%s allowed_tools=%s)",
        schedule_id,
        channel,
        delivered,
        agent,
        memory_mode,
        body.topic_id,
        body.allowed_tools,
    )
    return PlaybookFireResponse(
        accepted=True,
        schedule_id=schedule_id,
        channel=channel,
        agent=agent,
        memory=memory_mode,
        chats=delivered,
    )

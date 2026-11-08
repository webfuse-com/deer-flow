"""What actually reached the person: the delivery report and the notify mirror.

[argus patch #172] A send the platform refused is not a delivery.

The channel manager awaits a final answer's ``publish_outbound`` inline, so
the channel's ``send`` has finished before the turn is reported to Chronos
(#45). But ``Channel._on_outbound`` logs and swallows a send exception, so a
final answer that never posted (the Slack router refusing it, for example
``stack_not_enabled`` after ``slack-enable.sh --off``) was still reported
``delivered``. The Slack adapter now records why its post failed on the
outbound's metadata under :data:`DELIVERY_ERROR_KEY` before re-raising, and the
manager reads it back with :func:`delivery_error` and reports ``failed``.

Only Slack sets the key. Generalising it to ``Channel._on_outbound`` would also
surface Telegram send failures that are hidden today as Chronos errors, which
is a separate decision.

[argus patch #173] ``channel: notify`` with both channels mirrors the answer.

When the Slack router says the person wants notifications on Slack and
Telegram, the fire route publishes the run on Slack and names the Telegram
root chats under :data:`MIRROR_KEY` in the message metadata. After the final
answer is published, the manager publishes :func:`mirror_outbounds`: one
text-only message per chat, with no ``thread_ts`` and no binding, so a reply
to the mirror lands in that chat's main thread, not the run's.
"""

from __future__ import annotations

from app.channels.message_bus import InboundMessage, OutboundMessage

# Set by a channel's ``send`` on the outbound it could not post: Slack's (or
# the Slack router's) error code, else the exception type.
DELIVERY_ERROR_KEY = "argus_delivery_error"


def delivery_error(outbound: OutboundMessage) -> str | None:
    """Why *outbound* was not posted, or None when nothing said it failed."""
    value = (outbound.metadata or {}).get(DELIVERY_ERROR_KEY)
    return value if isinstance(value, str) and value else None


# Set by the fire route on a notify run's inbound metadata:
# {"channel": "telegram", "chat_ids": [...]}.
MIRROR_KEY = "argus_mirror"


def mirror_outbounds(msg: InboundMessage, thread_id: str, text: str) -> list[OutboundMessage]:
    """The text-only copies of *msg*'s final answer for the chats the fire route named."""
    mirror = (msg.metadata or {}).get(MIRROR_KEY)
    if not isinstance(mirror, dict):
        return []
    channel, chat_ids = mirror.get("channel"), mirror.get("chat_ids")
    if not isinstance(channel, str) or not channel or channel == msg.channel_name or not isinstance(chat_ids, list):
        return []
    targets = dict.fromkeys(chat_id for chat_id in chat_ids if isinstance(chat_id, str) and chat_id)
    return [OutboundMessage(channel_name=channel, chat_id=chat_id, thread_id=thread_id, text=text, metadata={"argus_mirror_of": msg.channel_name}) for chat_id in targets]

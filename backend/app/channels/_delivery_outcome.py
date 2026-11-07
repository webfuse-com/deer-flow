"""What actually reached the person, for the delivery report.

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
"""

from __future__ import annotations

from app.channels.message_bus import OutboundMessage

# Set by a channel's ``send`` on the outbound it could not post: Slack's (or
# the Slack router's) error code, else the exception type.
DELIVERY_ERROR_KEY = "argus_delivery_error"


def delivery_error(outbound: OutboundMessage) -> str | None:
    """Why *outbound* was not posted, or None when nothing said it failed."""
    value = (outbound.metadata or {}).get(DELIVERY_ERROR_KEY)
    return value if isinstance(value, str) and value else None

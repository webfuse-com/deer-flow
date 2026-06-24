"""Slack channel — connects via Socket Mode (no public IP needed)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from markdown_to_mrkdwn import SlackMarkdownConverter

from app.channels.base import Channel
from app.channels.message_bus import InboundMessageType, MessageBus, OutboundMessage, ResolvedAttachment

logger = logging.getLogger(__name__)

_slack_md_converter = SlackMarkdownConverter()


def _normalize_allowed_users(allowed_users: Any) -> set[str]:
    if allowed_users is None:
        return set()
    if isinstance(allowed_users, str):
        values = [allowed_users]
    elif isinstance(allowed_users, list | tuple | set):
        values = allowed_users
    else:
        logger.warning(
            "Slack allowed_users should be a list of Slack user IDs or a single Slack user ID string; treating %s as one string value",
            type(allowed_users).__name__,
        )
        values = [allowed_users]
    return {str(user_id) for user_id in values if str(user_id)}


class SlackChannel(Channel):
    """Slack IM channel using Socket Mode (WebSocket, no public IP).

    Configuration keys (in ``config.yaml`` under ``channels.slack``):
        - ``bot_token``: Slack Bot User OAuth Token (xoxb-...).
        - ``app_token``: Slack App-Level Token (xapp-...) for Socket Mode.
        - ``allowed_users``: (optional) List of allowed Slack user IDs, or a
          single Slack user ID string as shorthand. Empty = allow all. Other
          scalar values are treated as a single string with a warning.
    """

    def __init__(self, bus: MessageBus, config: dict[str, Any]) -> None:
        super().__init__(name="slack", bus=bus, config=config)
        self._socket_client = None
        self._web_client = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._allowed_users = _normalize_allowed_users(config.get("allowed_users", []))
        # [argus patch] progress acks to clean up when the answer lands, keyed by
        # (chat_id, thread_ts): {"ack_ts": <Working-on-it msg>, "react_ts": <the
        # message we put :eyes: on>}. Upstream leaves both behind.
        self._acks: dict[tuple[str, str], dict[str, str]] = {}

    async def start(self) -> None:
        if self._running:
            return

        try:
            from slack_sdk import WebClient
            from slack_sdk.socket_mode import SocketModeClient
            from slack_sdk.socket_mode.response import SocketModeResponse
        except ImportError:
            logger.error("slack-sdk is not installed. Install it with: uv add slack-sdk")
            return

        self._SocketModeResponse = SocketModeResponse

        bot_token = self.config.get("bot_token", "")
        app_token = self.config.get("app_token", "")

        if not bot_token or not app_token:
            logger.error("Slack channel requires bot_token and app_token")
            return

        self._web_client = WebClient(token=bot_token)
        self._socket_client = SocketModeClient(
            app_token=app_token,
            web_client=self._web_client,
        )
        self._loop = asyncio.get_event_loop()

        self._socket_client.socket_mode_request_listeners.append(self._on_socket_event)

        self._running = True
        self.bus.subscribe_outbound(self._on_outbound)

        # Start socket mode in background thread
        asyncio.get_event_loop().run_in_executor(None, self._socket_client.connect)
        logger.info("Slack channel started")

    async def stop(self) -> None:
        self._running = False
        self.bus.unsubscribe_outbound(self._on_outbound)
        if self._socket_client:
            self._socket_client.close()
            self._socket_client = None
        logger.info("Slack channel stopped")

    async def send(self, msg: OutboundMessage, *, _max_retries: int = 3) -> None:
        if not self._web_client:
            return

        kwargs: dict[str, Any] = {
            "channel": msg.chat_id,
            "text": _slack_md_converter.convert(msg.text),
        }
        if msg.thread_ts:
            kwargs["thread_ts"] = msg.thread_ts

        last_exc: Exception | None = None
        for attempt in range(_max_retries):
            try:
                await asyncio.to_thread(self._web_client.chat_postMessage, **kwargs)
                # Add a completion reaction to the thread root
                if msg.thread_ts:
                    await asyncio.to_thread(
                        self._add_reaction,
                        msg.chat_id,
                        msg.thread_ts,
                        "white_check_mark",
                    )
                    # [argus patch] clear the 'Working on it...' ack + :eyes:.
                    await asyncio.to_thread(
                        self._clear_acks, msg.chat_id, msg.thread_ts)
                return
            except Exception as exc:
                last_exc = exc
                if attempt < _max_retries - 1:
                    delay = 2**attempt  # 1s, 2s
                    logger.warning(
                        "[Slack] send failed (attempt %d/%d), retrying in %ds: %s",
                        attempt + 1,
                        _max_retries,
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)

        logger.error("[Slack] send failed after %d attempts: %s", _max_retries, last_exc)
        # Add failure reaction on error
        if msg.thread_ts:
            try:
                await asyncio.to_thread(
                    self._add_reaction,
                    msg.chat_id,
                    msg.thread_ts,
                    "x",
                )
            except Exception:
                pass
        if last_exc is None:
            raise RuntimeError("Slack send failed without an exception from any attempt")
        raise last_exc

    async def send_file(self, msg: OutboundMessage, attachment: ResolvedAttachment) -> bool:
        if not self._web_client:
            return False

        try:
            kwargs: dict[str, Any] = {
                "channel": msg.chat_id,
                "file": str(attachment.actual_path),
                "filename": attachment.filename,
                "title": attachment.filename,
            }
            if msg.thread_ts:
                kwargs["thread_ts"] = msg.thread_ts

            await asyncio.to_thread(self._web_client.files_upload_v2, **kwargs)
            logger.info("[Slack] file uploaded: %s to channel=%s", attachment.filename, msg.chat_id)
            return True
        except Exception:
            logger.exception("[Slack] failed to upload file: %s", attachment.filename)
            return False

    async def fetch_thread_context(self, chat_id: str, thread_ts: str,
                                   exclude_ts: str = "", limit: int = 30) -> str:
        """[argus patch] Earlier messages of a Slack thread, formatted as context.

        When a user replies under a message Pythia posted outside the agent (e.g.
        the minutes draft, which is a raw chat.postMessage with no DeerFlow
        thread), the manager has no conversation memory for that thread. This
        lets it pull the thread's prior messages via conversations.replies so the
        reply ("assign Nicholas to speaker 2") has the post it refers to.

        Best-effort: "" on any error (incl. missing_scope on a private channel
        without groups:history). Excludes `exclude_ts` (the current reply) and
        the bot's own "Working on it..." acks. Bot posts are kept (the minutes
        are a bot post and ARE the context)."""
        if not self._web_client or not chat_id or not thread_ts:
            return ""
        try:
            resp = await asyncio.to_thread(
                self._web_client.conversations_replies,
                channel=chat_id, ts=thread_ts, limit=limit)
        except Exception as exc:  # noqa: BLE001 — best-effort context
            logger.info("[Slack] thread context unavailable (%s)", exc)
            return ""
        msgs = resp.get("messages") or []
        lines: list[str] = []
        for m in msgs:
            ts = m.get("ts", "")
            if exclude_ts and ts == exclude_ts:
                continue
            body = (m.get("text") or "").strip()
            if not body or body.startswith(":hourglass"):  # skip the ack
                continue
            who = "Pythia" if m.get("bot_id") else f"<@{m.get('user', 'user')}>"
            lines.append(f"{who}: {body}")
        if not lines:
            return ""
        joined = "\n".join(lines)
        return ("[thread context — earlier messages in this Slack thread, for "
                f"reference]\n{joined}\n[end thread context]")

    # -- internal ----------------------------------------------------------

    def _add_reaction(self, channel_id: str, timestamp: str, emoji: str) -> None:
        """Add an emoji reaction to a message (best-effort, non-blocking)."""
        if not self._web_client:
            return
        try:
            self._web_client.reactions_add(
                channel=channel_id,
                timestamp=timestamp,
                name=emoji,
            )
        except Exception as exc:
            if "already_reacted" not in str(exc):
                logger.warning("[Slack] failed to add reaction %s: %s", emoji, exc)

    def _send_running_reply(self, channel_id: str, thread_ts: str,
                            react_ts: str = "") -> None:
        """Send a 'Working on it......' reply in the thread (called from SDK thread).

        [argus patch] Records the ack message ts (and the message we reacted to)
        so send() can clean both up once the real answer is posted."""
        if not self._web_client:
            return
        try:
            resp = self._web_client.chat_postMessage(
                channel=channel_id,
                text=":hourglass_flowing_sand: Working on it...",
                thread_ts=thread_ts,
            )
            self._acks[(channel_id, thread_ts)] = {
                "ack_ts": resp.get("ts", ""), "react_ts": react_ts}
            logger.info("[Slack] 'Working on it...' reply sent in channel=%s, thread_ts=%s", channel_id, thread_ts)
        except Exception:
            logger.exception("[Slack] failed to send running reply in channel=%s", channel_id)

    def _clear_acks(self, channel_id: str, thread_ts: str) -> None:
        """[argus patch] Delete the 'Working on it...' message and remove the
        :eyes: reaction once the answer is posted. Best-effort."""
        info = self._acks.pop((channel_id, thread_ts), None)
        if not info or not self._web_client:
            return
        if info.get("ack_ts"):
            try:
                self._web_client.chat_delete(channel=channel_id, ts=info["ack_ts"])
            except Exception as exc:  # noqa: BLE001
                logger.info("[Slack] could not delete ack (%s)", exc)
        if info.get("react_ts"):
            try:
                self._web_client.reactions_remove(
                    channel=channel_id, timestamp=info["react_ts"], name="eyes")
            except Exception as exc:  # noqa: BLE001
                logger.info("[Slack] could not remove eyes reaction (%s)", exc)

    def _on_socket_event(self, client, req) -> None:
        """Called by slack-sdk for each Socket Mode event."""
        try:
            # Acknowledge the event
            response = self._SocketModeResponse(envelope_id=req.envelope_id)
            client.send_socket_mode_response(response)

            event_type = req.type
            if event_type != "events_api":
                return

            event = req.payload.get("event", {})
            etype = event.get("type", "")

            # Handle message events (DM or @mention)
            if etype in ("message", "app_mention"):
                self._handle_message_event(event)

        except Exception:
            logger.exception("Error processing Slack event")

    def _handle_message_event(self, event: dict) -> None:
        # Ignore bot messages
        if event.get("bot_id") or event.get("subtype"):
            return

        user_id = event.get("user", "")

        # Check allowed users
        if self._allowed_users and user_id not in self._allowed_users:
            logger.debug("Ignoring message from non-allowed user: %s", user_id)
            return

        text = event.get("text", "").strip()
        if not text:
            return

        channel_id = event.get("channel", "")
        thread_ts = event.get("thread_ts") or event.get("ts", "")

        if text.startswith("/"):
            msg_type = InboundMessageType.COMMAND
        else:
            msg_type = InboundMessageType.CHAT

        # topic_id: use thread_ts as the topic identifier.
        # For threaded messages, thread_ts is the root message ts (shared topic).
        # For non-threaded messages, thread_ts is the message's own ts (new topic).
        inbound = self._make_inbound(
            chat_id=channel_id,
            user_id=user_id,
            text=text,
            msg_type=msg_type,
            thread_ts=thread_ts,
        )
        inbound.topic_id = thread_ts
        # [argus patch] stash the message's own ts so the manager can pull the
        # thread's earlier messages as context (and exclude this one) when the
        # reply lands on a thread that has no DeerFlow conversation yet — e.g. a
        # reply under the raw-posted minutes draft.
        inbound.metadata["event_ts"] = event.get("ts", "")

        if self._loop and self._loop.is_running():
            # Acknowledge with an eyes reaction on the user's message.
            react_ts = event.get("ts", thread_ts)
            self._add_reaction(channel_id, react_ts, "eyes")
            # Send "running" reply first (fire-and-forget from SDK thread).
            # [argus patch] pass react_ts so both acks are cleaned up on answer.
            self._send_running_reply(channel_id, thread_ts, react_ts)
            asyncio.run_coroutine_threadsafe(self.bus.publish_inbound(inbound), self._loop)

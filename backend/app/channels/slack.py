"""Slack channel — connects via Socket Mode (no public IP needed)."""

from __future__ import annotations

import asyncio
import html
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx
from markdown_to_mrkdwn import SlackMarkdownConverter

from app.channels._delivery_outcome import DELIVERY_ERROR_KEY
from app.channels.allowed_users import parse_allowed_users, parse_string_or_int_user_id
from app.channels.base import Channel, ChannelStopTimeout
from app.channels.commands import is_known_channel_command
from app.channels.connection_identity import attach_connection_identity
from app.channels.message_bus import (
    INBOUND_FILE_CONTENT_KEY,
    InboundMessage,
    InboundMessageType,
    InboundReservation,
    MessageBus,
    OutboundMessage,
    ResolvedAttachment,
)
from deerflow.uploads.manager import is_upload_staging_file, normalize_filename

logger = logging.getLogger(__name__)
SLACK_STOP_TIMEOUT_SECONDS = 5.0
# Match the manager's URL download ceiling. Slack workspace limits may be higher;
# this keeps a single upload from exhausting Gateway memory.
SLACK_MAX_INBOUND_FILE_BYTES = 50 * 1024 * 1024
# Cap total in-memory bytes for one file_share message (N files × per-file max).
SLACK_MAX_INBOUND_MESSAGE_BYTES = SLACK_MAX_INBOUND_FILE_BYTES
# Private download URLs are Slack-hosted; gate Bearer fetches to this suffix.
SLACK_ALLOWED_FILE_HOST_SUFFIXES = frozenset({"files.slack.com"})
# User-authored subtypes we still want to turn into chat turns. Everything else
# (edits, deletes, joins, bot_message, thread_broadcast, …) stays ignored so we
# do not double-fire or treat platform noise as a user message.
_SLACK_ACCEPTED_MESSAGE_SUBTYPES = frozenset({"file_share"})
# [argus patch #171] ``transport: relay``: no Socket Mode client; a router
# holds the Slack app, delivers events and serves the Web API at relay_url.
SLACK_TRANSPORT_RELAY = "relay"
# The audiences a relayed event can come from (the router decides which).
SLACK_RELAY_TIERS = frozenset({"dm", "public", "private"})
# A bot's display name in hydrated thread context is capped at this length.
_SLACK_BOT_LABEL_MAX_CHARS = 80
# [argus patch #172] The router's synthetic method naming the stack's owner,
# their DM with the app and where they want notifications.
SLACK_ARGUS_OWNER_METHOD = "argus.owner"
# A fire waits at most this long for it; slack_sdk's own timeout is 30 s.
_SLACK_ARGUS_OWNER_TIMEOUT_SECONDS = 10.0
# Prefix of an owner_conversation() error that means no answer at all.
SLACK_ROUTER_UNREACHABLE = "router_unreachable"
# The notification channels argus.owner may name.
_SLACK_OWNER_CHANNELS = ("slack", "telegram")
# A real Slack message ts. Synthetic topics (sched:<id>, hook:..., notify:...)
# never match, so they are not Slack threads to hydrate.
_SLACK_TS_RE = re.compile(r"\d+\.\d+")

_slack_md_converter = SlackMarkdownConverter()


@dataclass(frozen=True)
class SlackOwner:
    """[argus patch #172] The router's ``argus.owner`` answer for this stack.

    ``enabled``: Slack is enabled for the stack. ``user_id``/``dm``: the
    owner's Slack user and their DM with the app (empty when unresolved).
    ``channels``: where the owner wants notifications, in order (the router
    applies the stack's preference; read by ``channel: notify``, #173).
    """

    enabled: bool
    user_id: str
    dm: str
    channels: tuple[str, ...] = ()


def _slack_error_code(exc: BaseException) -> str:
    """[argus patch #172] Slack's (or the router's) error code for a refused call, else the exception type."""
    response = getattr(exc, "response", None)
    getter = getattr(response, "get", None)
    code = getter("error") if callable(getter) else None
    return code if isinstance(code, str) and code else type(exc).__name__


def _response_value(response: Any, key: str) -> Any:
    """A field of a Web API answer (a ``SlackResponse`` or a plain dict)."""
    getter = getattr(response, "get", None)
    return getter(key) if callable(getter) else None


@dataclass(frozen=True)
class _RelayDelivery:
    """[argus patch #171] What the router said about one relayed event."""

    metadata: dict[str, Any] = field(default_factory=dict)
    agent_name: str | None = None
    # Channel commands (/memory, /agent, /new, ...) act on the owner's state,
    # so only the owner's DM may issue them; elsewhere they are chat text.
    commands_allowed: bool = False


def _relay_base_url(value: Any) -> str | None:
    """[argus patch #171] ``relay_url`` as a slack_sdk ``base_url``.

    slack_sdk joins the method name onto ``base_url`` with ``urljoin``, so a
    URL without a trailing slash would drop its last path segment
    (``http://router:8080/api`` + ``chat.postMessage`` ->
    ``http://router:8080/chat.postMessage``). None for a missing or non-HTTP URL.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    url = value.strip()
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return url if url.endswith("/") else f"{url}/"


def _parse_tier_agents(value: Any) -> dict[str, str]:
    """[argus patch #171] ``tier_agents: {dm|public|private: <agent>}``."""
    if not isinstance(value, dict):
        return {}
    agents: dict[str, str] = {}
    for tier, agent in value.items():
        if tier not in SLACK_RELAY_TIERS:
            logger.warning("[Slack] tier_agents.%s ignored: tiers are %s", tier, ", ".join(sorted(SLACK_RELAY_TIERS)))
            continue
        if isinstance(agent, str) and agent.strip():
            agents[tier] = agent.strip()
    return agents


def _slack_bot_label(message: dict) -> str:
    """[argus patch #171, amends #22] Name a bot message in thread context.

    The bot's own name: the message's ``username`` (a persona such as "Atlas
    for Nicholas" set at post time), then ``bot_profile.name`` (the app's bot
    name, e.g. "Pythia"), then "a bot". Whitespace is collapsed and the name
    capped so a bot name cannot reshape the context block.
    """
    profile = message.get("bot_profile")
    profile_name = profile.get("name") if isinstance(profile, dict) else None
    for candidate in (message.get("username"), profile_name):
        if isinstance(candidate, str):
            name = " ".join(candidate.split())[:_SLACK_BOT_LABEL_MAX_CHARS]
            if name:
                return name
    return "a bot"


def _escape_slack_text(text: str) -> str:
    """Escape Slack's reserved characters (``&``, ``<``, ``>``) in raw message text,
    except a ``>`` at the very start of a line -- Slack's own blockquote marker.

    Slack requires callers to replace these with their HTML entity equivalents
    (``&amp;``, ``&lt;``, ``&gt;``) before sending message text -- an unescaped
    ``<...>`` triggers Slack's own mention/link syntax (e.g. ``<@USERID>``,
    ``<http://url|label>``). See:
    https://api.slack.com/reference/surfaces/formatting#escaping

    This MUST run before ``_slack_md_converter.convert()``, not after: the
    converter emits its own mrkdwn link syntax (``<url|label>``) for real
    markdown links, and that generated syntax must reach Slack unescaped.
    Escaping the raw input first -- and leaving the converter's own output
    alone -- satisfies both requirements. ``html.escape(..., quote=False)``
    replaces ``&`` before ``<``/``>``, so the entities it introduces are never
    re-escaped.

    Only ``&`` and ``<`` neutralize Slack's ``<...>`` mention/link syntax; a
    ``>`` is special to Slack only at the start of a line, where the mrkdwn
    converter passes it through unchanged as a blockquote marker. Escaping
    every ``>`` would turn a quoted line into visible ``&gt;`` text instead of
    a rendered blockquote, so a line-leading ``>`` is restored to a literal
    ``>`` after escaping; a ``>`` anywhere else in the text still escapes.
    """
    escaped = html.escape(text, quote=False)
    return re.sub(r"(?m)^&gt;", ">", escaped)


def _parse_allowed_users(allowed_users: Any) -> frozenset[str] | None:
    """Parse Slack user IDs with the shared fail-closed allowlist semantics."""
    return parse_allowed_users(
        allowed_users,
        parse_user_id=parse_string_or_int_user_id,
        logger=logger,
        channel_name="Slack",
        expected_id="a Slack user ID such as U123456",
        warn_on_scalar_separators=True,
    )


def _strip_leading_slack_bot_mention(text: str, bot_user_id: str | None) -> str:
    if not bot_user_id:
        return text
    if not text.startswith("<@"):
        return text
    end = text.find(">")
    if end <= 2:
        return text
    mentioned_user_id = text[2:end].split("|", 1)[0].lstrip("!")
    if mentioned_user_id != bot_user_id:
        return text
    return text[end + 1 :].lstrip()


def _unescape_slack_text(text: str) -> str:
    """Decode the three entities Slack escapes in message text.

    Slack sends a user-typed ``&``, ``<`` and ``>`` as ``&amp;``, ``&lt;`` and
    ``&gt;`` so that raw ``<...>`` always marks a control sequence (mention,
    link). ``&amp;`` is decoded last so an escaped entity is decoded only once.
    """
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


class SlackChannel(Channel):
    """Slack IM channel using Socket Mode (WebSocket, no public IP).

    Configuration keys (in ``config.yaml`` under ``channels.slack``):
        - ``bot_token``: Slack Bot User OAuth Token (xoxb-...).
        - ``app_token``: Slack App-Level Token (xapp-...) for Socket Mode.
        - ``allowed_users``: (optional) List of allowed Slack user IDs, or a
          single Slack user ID as shorthand. Unset, empty or blank = allow
          all. A non-empty value without a valid ID denies everyone.

    [argus patch #171] Relay transport (``transport: relay``):
        - ``relay_url``: the router's Web API base (``http://router:8080/api/``).
        - ``relay_token``: this gateway's router key, sent as the bot token.
        - ``tier_agents``: (optional) ``{dm|public|private: <agent>}``, the
          agent a relayed message of that tier runs; a missing tier keeps the
          channel default.
      No Socket Mode client is created and ``bot_token``/``app_token`` are not
      read. Events arrive through :meth:`relay_event`; ``allowed_users`` does
      not apply to them because the router has already authorized the sender.
    """

    def __init__(self, bus: MessageBus, config: dict[str, Any]) -> None:
        super().__init__(name="slack", bus=bus, config=config)
        self._socket_client = None
        self._socket_connect_future: asyncio.Future[None] | None = None
        self._socket_close_task: asyncio.Task[None] | None = None
        self._web_client = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._allowed_users = _parse_allowed_users(config.get("allowed_users"))
        self._web_client_factory = config.get("web_client_factory")
        # [argus patch #171] relay transport: every WebClient talks to the router.
        self._relay = str(config.get("transport") or "").strip().lower() == SLACK_TRANSPORT_RELAY
        self._relay_base_url = _relay_base_url(config.get("relay_url")) if self._relay else None
        self._tier_agents = _parse_tier_agents(config.get("tier_agents")) if self._relay else {}
        self._connection_web_clients: dict[str, tuple[str, Any]] = {}
        configured_bot_user_id = config.get("bot_user_id")
        self._bot_user_id = str(configured_bot_user_id).lstrip("@") if configured_bot_user_id else None
        # [argus patch #23] progress acks to clean up when the answer lands, keyed
        # by (chat_id, thread_ts): {"ack_ts": <Working-on-it msg>, "react_ts": <the
        # message we put :eyes: on>}. Upstream leaves both behind on every turn.
        self._acks: dict[tuple[str, str], dict[str, str]] = {}

    async def start(self) -> None:
        if self._running:
            return
        if self._socket_client is not None or self._socket_close_task is not None:
            # A timed-out or cancelled stop still owns its SDK work. Reuse its
            # bounded wait rather than replacing a client that can still dial.
            await self.stop()

        try:
            from slack_sdk import WebClient
            from slack_sdk.socket_mode import SocketModeClient
            from slack_sdk.socket_mode.response import SocketModeResponse
        except ImportError:
            logger.error("slack-sdk is not installed. Install it with: uv add slack-sdk")
            return

        self._SocketModeResponse = SocketModeResponse
        if self._web_client_factory is None:
            self._web_client_factory = WebClient

        if self._relay:
            await self._start_relay()
            return

        bot_token = self.config.get("bot_token", "")
        app_token = self.config.get("app_token", "")

        if self.config.get("event_delivery") == "http":
            logger.error("Slack HTTP Events mode is not supported by this channel adapter; use Socket Mode with app_token")
            return

        if not bot_token or not app_token:
            logger.error("Slack channel requires bot_token and app_token")
            return

        await self._initialize_operator_web_client(str(bot_token))
        self._socket_client = SocketModeClient(
            app_token=app_token,
            web_client=self._web_client,
        )
        self._loop = asyncio.get_event_loop()

        self._socket_client.socket_mode_request_listeners.append(self._on_socket_event)

        self._open_threadsafe_future_intake()
        self._running = True
        self.bus.subscribe_outbound(self._on_outbound)

        # Start socket mode in a background thread.
        # ``_connect_socket_mode`` logs startup failures. Retain the future so
        # stop() can wait for an in-flight dial before closing its socket.
        socket_client = self._socket_client
        self._socket_connect_future = asyncio.get_event_loop().run_in_executor(None, self._connect_socket_mode, socket_client)
        logger.info("Slack channel started")

    async def _start_relay(self) -> None:
        """[argus patch #171] Start without Socket Mode.

        The router serves the Web API at ``relay_url`` and answers
        ``auth.test`` with the app's bot user id, so the operator WebClient is
        built exactly as in Socket Mode, only aimed at the router. Intake,
        the running flag and the outbound subscription are the same as in
        Socket Mode; events arrive through :meth:`relay_event`.
        """
        relay_token = str(self.config.get("relay_token") or "").strip()
        if not self._relay_base_url or not relay_token:
            logger.error("Slack relay transport requires relay_url (an http(s) URL) and relay_token")
            return
        await self._initialize_operator_web_client(relay_token)
        self._loop = asyncio.get_running_loop()
        self._open_threadsafe_future_intake()
        self._running = True
        self.bus.subscribe_outbound(self._on_outbound)
        logger.info("Slack channel started (relay transport, Web API at %s)", self._relay_base_url)

    @property
    def uses_relay_transport(self) -> bool:
        """[argus patch #171] Whether events arrive through :meth:`relay_event`."""
        return self._relay

    async def owner_conversation(self) -> tuple[SlackOwner | None, str]:
        """[argus patch #172] Ask the router for this stack's owner: ``(owner, error)``.

        One call of the router's synthetic ``argus.owner`` through the relay
        WebClient. The router answers it even when Slack is not enabled for
        the stack; no Slack scope, email or directory method reaches the
        gateway. ``error`` is the router's error code for a refusal, or
        starts with :data:`SLACK_ROUTER_UNREACHABLE` when there was no answer.
        """
        if not self._relay or self._web_client is None:
            return None, "slack is not running on the relay transport"
        try:
            resp = await asyncio.wait_for(asyncio.to_thread(self._web_client.api_call, SLACK_ARGUS_OWNER_METHOD), timeout=_SLACK_ARGUS_OWNER_TIMEOUT_SECONDS)
        except TimeoutError:
            return None, f"{SLACK_ROUTER_UNREACHABLE}: no answer in {_SLACK_ARGUS_OWNER_TIMEOUT_SECONDS:g} s"
        except Exception as exc:
            code = _slack_error_code(exc)
            if code == type(exc).__name__:
                # No error code: the request itself failed (connection, HTTP, JSON).
                return None, f"{SLACK_ROUTER_UNREACHABLE}: {code}"
            return None, code
        if _response_value(resp, "ok") is not True:
            return None, "malformed argus.owner answer"
        channels = _response_value(resp, "channels")
        listed = channels if isinstance(channels, list | tuple) else ()
        return (
            SlackOwner(
                enabled=_response_value(resp, "enabled") is True,
                user_id=str(_response_value(resp, "user_id") or ""),
                dm=str(_response_value(resp, "dm") or ""),
                channels=tuple(dict.fromkeys(c for c in listed if c in _SLACK_OWNER_CHANNELS)),
            ),
            "",
        )

    async def relay_event(self, envelope: dict, argus: dict) -> bool:
        """[argus patch #171] Handle one Events API envelope the Slack router delivered.

        The event takes the same path as a Socket Mode event
        (``_handle_message_event``), minus the socket ack, and on a worker
        thread as the SDK's own callback does: the handler posts the eyes
        reaction and the progress reply synchronously. ``argus`` is the
        router's verdict: ``tier`` (dm|public|private) picks the agent from
        ``tier_agents`` and rides the message metadata with ``trigger_user``
        and ``persona``. The router has already authorized the sender, so
        ``allowed_users`` does not apply.

        ``action: stop`` is refused: the channel manager has no path to cancel
        a run a channel started (Slack waits on ``runs.wait`` with
        ``on_disconnect: continue``).

        Returns whether the event became a queued inbound message.
        """
        if not self._relay or not self._running:
            logger.warning("[Slack] relay event refused: the channel is not running on the relay transport")
            return False
        if not isinstance(envelope, dict) or not isinstance(argus, dict):
            return False
        event = envelope.get("event")
        if not isinstance(event, dict):
            return False
        action = argus.get("action") or "message"
        if action == "stop":
            logger.info(
                "[Slack] relay stop for channel=%s thread_ts=%s not applied: the channel manager cannot cancel a channel run",
                event.get("channel"),
                event.get("thread_ts") or event.get("ts"),
            )
            return False
        if action != "message":
            logger.warning("[Slack] relay event with unknown action %r refused", action)
            return False
        tier = argus.get("tier")
        if tier not in SLACK_RELAY_TIERS:
            # Fail closed: the tier chooses the agent, and an unknown tier must
            # not fall back to the channel default (the owner's full agent).
            logger.warning("[Slack] relay event refused: unknown tier %r", tier)
            return False
        if event.get("type") not in ("message", "app_mention"):
            return False

        metadata: dict[str, Any] = {"tier": tier}
        for key in ("trigger_user", "persona"):
            value = argus.get(key)
            if isinstance(value, str) and value:
                metadata[key] = value
        delivery = _RelayDelivery(
            metadata=metadata,
            agent_name=self._tier_agents.get(tier),
            commands_allowed=tier == "dm",
        )
        team_id = envelope.get("team_id") or envelope.get("team") or event.get("team")
        return await asyncio.to_thread(self._handle_message_event, event, team_id=team_id, relay=delivery)

    def _connect_socket_mode(self, socket_client: Any) -> None:
        """Connect one start attempt's Socket Mode client if it is still current.

        Mirrors the Telegram and Discord channels, whose background threads log
        their errors instead of letting them vanish with the thread.
        """
        if not self._running or socket_client is not self._socket_client:
            return
        try:
            socket_client.connect()
        except Exception:
            logger.exception("[Slack] Socket Mode connection failed; the channel will not receive events")

    async def stop(self) -> None:
        self._running = False
        self.bus.unsubscribe_outbound(self._on_outbound)
        await self._close_and_drain_threadsafe_futures()
        # The SDK can publish a session after close() if connect() is already
        # waiting for its URL. One owned task drains that connect and then
        # closes off-loop. A timeout or cancellation keeps it available for
        # retry; cancelling an executor future would not stop its thread.
        if self._socket_close_task is None and self._socket_client is not None:
            self._socket_close_task = asyncio.create_task(self._close_socket_client(self._socket_client, self._socket_connect_future))
        close_task = self._socket_close_task
        if close_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(close_task), timeout=SLACK_STOP_TIMEOUT_SECONDS)
            except TimeoutError as exc:
                if close_task.done():
                    close_task.result()
                else:
                    raise ChannelStopTimeout("Slack Socket Mode cleanup is still running") from exc
            finally:
                if close_task.done() and self._socket_close_task is close_task:
                    self._socket_close_task = None
        logger.info("Slack channel stopped")

    async def _close_socket_client(self, socket_client: Any, connect_future: asyncio.Future[None] | None) -> None:
        if connect_future is not None:
            await asyncio.shield(connect_future)
        await asyncio.to_thread(socket_client.close)
        if self._socket_client is socket_client:
            self._socket_client = None
        if self._socket_connect_future is connect_future:
            self._socket_connect_future = None

    async def send(self, msg: OutboundMessage, *, _max_retries: int = 3) -> None:
        web_client = await self._get_web_client_for_message(msg)
        if not web_client:
            # [argus patch #172] Nothing was posted; the delivery report must say so.
            msg.metadata[DELIVERY_ERROR_KEY] = "no Slack web client"
            return

        kwargs: dict[str, Any] = {
            "channel": msg.chat_id,
            "text": _slack_md_converter.convert(_escape_slack_text(msg.text)),
        }
        if msg.thread_ts:
            kwargs["thread_ts"] = msg.thread_ts
        # [argus patch #172] Every Slack event carries a thread_ts, so on the
        # relay transport an outbound without one answers a synthetic inbound
        # (a playbook fire, a notify, their error replies). It posts as a new
        # top-level message in the owner's DM (the only place the router
        # allows one) and that message roots the run's thread.
        root_post = self._relay and not msg.thread_ts
        posted: dict[str, str] = {}

        async def post_message() -> None:
            resp = await asyncio.to_thread(web_client.chat_postMessage, **kwargs)
            if root_post:
                posted["ts"] = str(_response_value(resp, "ts") or "")
            # Add a completion reaction to the thread root
            if msg.thread_ts:
                await asyncio.to_thread(
                    self._add_reaction_with_client,
                    web_client,
                    msg.chat_id,
                    msg.thread_ts,
                    "white_check_mark",
                )
                # [argus patch #23] clear the 'Working on it...' ack + :eyes:
                # reaction now that the real answer has posted.
                await asyncio.to_thread(self._clear_acks, web_client, msg.chat_id, msg.thread_ts)

        try:
            await self._send_with_retry(
                post_message,
                max_retries=_max_retries,
                log_prefix="[Slack]",
            )
        except Exception as exc:
            # [argus patch #172] _on_outbound swallows this; the manager reads
            # the reason back and reports the turn failed, not delivered.
            msg.metadata[DELIVERY_ERROR_KEY] = _slack_error_code(exc)
            # Add failure reaction on error
            if msg.thread_ts:
                try:
                    await asyncio.to_thread(
                        self._add_reaction_with_client,
                        web_client,
                        msg.chat_id,
                        msg.thread_ts,
                        "x",
                    )
                except Exception:
                    pass
            raise
        if posted.get("ts"):
            # [argus patch #172] Outside the retried post, so a store error can
            # never post twice. Set thread_ts only now, after the reaction and
            # ack block, so Atlas's own root gets no check mark and the files
            # _on_outbound sends next land in the root's thread.
            await self._bind_root_post(msg, posted["ts"])
            msg.thread_ts = posted["ts"]

    async def _bind_root_post(self, msg: OutboundMessage, root_ts: str) -> None:
        """[argus patch #172] Bind ``(slack, DM, root_ts)`` to the run's thread.

        A reply under the root arrives with ``thread_ts = root_ts`` (its topic),
        so it continues the run's DeerFlow thread instead of starting a new
        one. The same alias Feishu's ``_remember_thread_mapping`` writes for
        its cards. Best effort: logged, never raised, never retried.
        """
        store = self.config.get("channel_store")
        if store is None or not msg.thread_id:
            return
        try:
            await store.set_thread_id(self.name, msg.chat_id, msg.thread_id, topic_id=root_ts, user_id=str(msg.metadata.get("user_id") or ""))
        except Exception:
            logger.exception("[Slack] could not bind root message %s in %s to thread %s", root_ts, msg.chat_id, msg.thread_id)

    async def send_file(self, msg: OutboundMessage, attachment: ResolvedAttachment) -> bool:
        web_client = await self._get_web_client_for_message(msg)
        if not web_client:
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

            await asyncio.to_thread(web_client.files_upload_v2, **kwargs)
            logger.info("[Slack] file uploaded: %s to channel=%s", attachment.filename, msg.chat_id)
            return True
        except Exception:
            logger.exception("[Slack] failed to upload file: %s", attachment.filename)
            return False

    async def fetch_thread_context(self, chat_id: str, thread_ts: str, exclude_ts: str = "", limit: int = 30) -> str:
        """[argus patch #22] Earlier messages of a Slack thread, formatted as context.

        When a user replies under a message Pythia posted outside the agent (e.g.
        the minutes draft, which is a raw chat.postMessage with no DeerFlow
        thread), the manager has no conversation memory for that thread. This
        lets it pull the thread's prior messages via conversations.replies so the
        reply ("assign Nicholas to speaker 2") has the post it refers to.

        Best-effort: "" on any error (incl. missing_scope on a private channel
        without groups:history). Excludes `exclude_ts` (the current reply) and
        the bot's own "Working on it..." acks. Bot posts are kept (the minutes
        are a bot post and ARE the context), each labelled by its own name
        ([argus patch #171]: Pythia, an Atlas persona, any other bot).

        [argus patch #172] A synthetic topic (``sched:<id>``, ``hook:...``) is
        no Slack thread: "" without a call."""
        if not self._web_client or not chat_id or not thread_ts:
            return ""
        if not _SLACK_TS_RE.fullmatch(thread_ts):
            return ""
        try:
            resp = await asyncio.to_thread(self._web_client.conversations_replies, channel=chat_id, ts=thread_ts, limit=limit)
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
            who = _slack_bot_label(m) if m.get("bot_id") else f"<@{m.get('user', 'user')}>"
            lines.append(f"{who}: {body}")
        if not lines:
            return ""
        joined = "\n".join(lines)
        return f"[thread context — earlier messages in this Slack thread, for reference]\n{joined}\n[end thread context]"

    # -- internal ----------------------------------------------------------

    def _new_web_client(self, token: str):
        """[argus patch #171] On the relay transport every WebClient is aimed at the router."""
        if self._relay_base_url:
            return self._web_client_factory(token=token, base_url=self._relay_base_url)
        return self._web_client_factory(token=token)

    async def _initialize_operator_web_client(self, bot_token: str) -> None:
        self._web_client = self._new_web_client(bot_token)
        if self._bot_user_id is not None:
            return
        try:
            auth_info = await asyncio.to_thread(self._web_client.auth_test)
            user_id = auth_info.get("user_id") if isinstance(auth_info, dict) else None
            if user_id is None:
                auth_get = getattr(auth_info, "get", None)
                user_id = auth_get("user_id") if callable(auth_get) else None
            if isinstance(user_id, str) and user_id:
                self._bot_user_id = user_id
        except Exception:
            logger.warning("[Slack] failed to resolve bot user id; app mention text may include the bot mention", exc_info=True)

    async def _web_client_for_connection(self, connection_id: str | None):
        """Resolve a WebClient for *connection_id*, falling back to the operator client.

        WebClient keeps its own HTTP session and rate-limit state, so reuse one
        per connection until its token changes. Shared by outbound sends and
        inbound private-file downloads so the cache cannot drift.
        """
        if connection_id and self._connection_repo is not None:
            credentials = await self._connection_repo.get_credentials(connection_id)
            access_token = credentials.get("access_token") if credentials else None
            if not access_token:
                return self._web_client
            cached = self._connection_web_clients.get(connection_id)
            if cached is not None and cached[0] == access_token:
                return cached[1]
            if self._web_client_factory is None:
                from slack_sdk import WebClient

                self._web_client_factory = WebClient
            web_client = self._new_web_client(access_token)
            self._connection_web_clients[connection_id] = (access_token, web_client)
            return web_client
        return self._web_client

    async def _get_web_client_for_message(self, msg: OutboundMessage):
        return await self._web_client_for_connection(msg.connection_id)

    @staticmethod
    def _add_reaction_with_client(web_client, channel_id: str, timestamp: str, emoji: str) -> None:
        try:
            web_client.reactions_add(
                channel=channel_id,
                timestamp=timestamp,
                name=emoji,
            )
        except Exception as exc:
            if "already_reacted" not in str(exc):
                logger.warning("[Slack] failed to add reaction %s: %s", emoji, exc)

    def _add_reaction(self, channel_id: str, timestamp: str, emoji: str) -> None:
        """Add an emoji reaction to a message (best-effort, non-blocking)."""
        if not self._web_client:
            return
        self._add_reaction_with_client(self._web_client, channel_id, timestamp, emoji)

    def _send_running_reply(self, channel_id: str, thread_ts: str, react_ts: str = "") -> None:
        """Send a 'Working on it......' reply in the thread (called from SDK thread).

        [argus patch #23] Records the ack message ts (and the message we reacted
        to) so send() can clean both up once the real answer is posted."""
        if not self._web_client:
            return
        try:
            resp = self._web_client.chat_postMessage(
                channel=channel_id,
                text=":hourglass_flowing_sand: Working on it...",
                thread_ts=thread_ts,
            )
            self._acks[(channel_id, thread_ts)] = {"ack_ts": resp.get("ts", ""), "react_ts": react_ts}
            logger.info("[Slack] 'Working on it...' reply sent in channel=%s, thread_ts=%s", channel_id, thread_ts)
        except Exception:
            logger.exception("[Slack] failed to send running reply in channel=%s", channel_id)

    def _clear_acks(self, web_client, channel_id: str, thread_ts: str) -> None:
        """[argus patch #23] Delete the 'Working on it...' message and remove the
        :eyes: reaction once the answer is posted. Best-effort; runs in a worker
        thread (called via asyncio.to_thread from send())."""
        info = self._acks.pop((channel_id, thread_ts), None)
        if not info or web_client is None:
            return
        if info.get("ack_ts"):
            try:
                web_client.chat_delete(channel=channel_id, ts=info["ack_ts"])
            except Exception as exc:  # noqa: BLE001
                logger.info("[Slack] could not delete ack (%s)", exc)
        if info.get("react_ts"):
            try:
                web_client.reactions_remove(channel=channel_id, timestamp=info["react_ts"], name="eyes")
            except Exception as exc:  # noqa: BLE001
                logger.info("[Slack] could not remove eyes reaction (%s)", exc)

    def _on_socket_event(self, client, req) -> None:
        """Called by slack-sdk for each Socket Mode event."""
        if not self._running:
            return
        try:
            # Acknowledge the event
            response = self._SocketModeResponse(envelope_id=req.envelope_id)
            client.send_socket_mode_response(response)

            event_type = req.type
            if event_type != "events_api":
                return

            if self._bot_user_id is None:
                authorization = next((item for item in req.payload.get("authorizations", []) if isinstance(item, dict)), None)
                user_id = authorization.get("user_id") if authorization else None
                if isinstance(user_id, str) and user_id:
                    self._bot_user_id = user_id

            event = req.payload.get("event", {})
            etype = event.get("type", "")

            # Handle message events (DM or @mention)
            if etype in ("message", "app_mention"):
                self._handle_message_event(
                    event,
                    team_id=req.payload.get("team_id") or req.payload.get("team") or event.get("team"),
                )

        except Exception:
            logger.exception("Error processing Slack event")

    def _handle_message_event(self, event: dict, *, team_id: str | None = None, relay: _RelayDelivery | None = None) -> bool:
        """Turn one Slack message event into an inbound message.

        Returns whether it became work (a reserved inbound message or a
        scheduled connect bind). [argus patch #171] ``relay`` carries the
        router's verdict for a relayed event: ``allowed_users`` is skipped,
        commands only count where the router allows them, and the tier's
        agent and metadata ride the inbound message.
        """
        # Ignore bot traffic and platform subtypes we do not handle as chat.
        # file_share is a user upload (optional caption + files[]) and must not
        # be dropped just because Slack sets subtype.
        subtype = event.get("subtype")
        if event.get("bot_id") or (subtype and subtype not in _SLACK_ACCEPTED_MESSAGE_SUBTYPES):
            return False

        user_id = event.get("user", "")

        text = event.get("text", "").strip()
        if event.get("type") == "app_mention":
            text = _strip_leading_slack_bot_mention(text, self._bot_user_id)
        # Decode after mention stripping: a real mention is a raw <@...>, while
        # a user-typed "<@...>" arrives as &lt;@...&gt; and must stay text.
        text = _unescape_slack_text(text)
        files = self._extract_inbound_files(event)
        if not text and not files:
            return False

        connect_code = self._pending_connect_code(text)
        if connect_code:
            if self._loop and self._loop.is_running():
                scheduled = self._submit_threadsafe_coroutine(
                    self._bind_connection_from_connect_code(
                        event=event,
                        team_id=str(team_id or ""),
                        code=connect_code,
                    ),
                    self._loop,
                    name="bind_connection",
                    msg_id=event.get("ts"),
                )
                if not scheduled:
                    logger.info("[Slack] main loop stopped before channel connection bind could be scheduled")
                return scheduled
            return False

        # Check allowed users after connect-code handling so browser-initiated
        # binding can bootstrap a new external identity.
        # [argus patch #171] A relayed event's sender was authorized by the router.
        if relay is None and self._allowed_users is not None and user_id not in self._allowed_users:
            logger.debug("Ignoring message from non-allowed user: %s", user_id)
            return False

        channel_id = event.get("channel", "")
        thread_ts = event.get("thread_ts") or event.get("ts", "")

        if is_known_channel_command(text) and (relay is None or relay.commands_allowed):
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
            files=files,
            metadata={
                # team_id is already resolved (payload team_id/team, else event team) by the caller.
                "team_id": team_id,
                "message_id": event.get("ts"),
                "client_msg_id": event.get("client_msg_id"),
            },
        )
        inbound.topic_id = thread_ts
        # [argus patch #22] stash the message's own ts so the manager can pull the
        # thread's earlier messages as context (and exclude this one) when the
        # reply lands on a thread that has no DeerFlow conversation yet — e.g. a
        # reply under the raw-posted minutes draft.
        inbound.metadata["event_ts"] = event.get("ts", "")
        if relay is not None:
            # [argus patch #171] tier, trigger_user, persona; the tier's agent
            # wins over the channel default for this message only.
            inbound.metadata.update(relay.metadata)
            if relay.agent_name:
                inbound.agent_name = relay.agent_name

        if self._loop and self._loop.is_running():
            reservation = self._reserve_inbound(inbound)
            if reservation is None:
                return False
            # Acknowledge with an eyes reaction on the user's message.
            react_ts = event.get("ts", thread_ts)
            self._add_reaction(channel_id, react_ts, "eyes")
            # Send "running" reply first (fire-and-forget from SDK thread).
            # [argus patch #23] pass react_ts so both acks are cleaned up on answer.
            self._send_running_reply(channel_id, thread_ts, react_ts)
            try:
                if self._connection_repo is None:
                    # Reservation bounds callbacks scheduled from the SDK
                    # thread; no coroutine/Future waits for queue capacity.
                    self._loop.call_soon_threadsafe(self._commit_reserved_inbound, reservation, inbound)
                    return True
                else:
                    scheduled = self._submit_threadsafe_coroutine(
                        self._publish_inbound_with_connection(inbound, reservation=reservation, team_id=team_id),
                        self._loop,
                        name="publish_inbound",
                        msg_id=event.get("ts", thread_ts),
                        reservation=reservation,
                    )
                    if not scheduled:
                        logger.info("[Slack] main loop stopped before reserved inbound could be scheduled")
                    return scheduled
            except RuntimeError:
                reservation.release()
                logger.info("[Slack] main loop stopped before reserved inbound could be scheduled")
        return False

    async def receive_file(self, msg: InboundMessage, thread_id: str, *, user_id: str | None = None) -> InboundMessage:
        """Download inbound Slack file_share attachments for the shared upload pipeline.

        Slack private download URLs require the bot token in an Authorization
        header, so this adapter never hands those URLs to the generic HTTP
        reader. Bytes cross to ``ChannelManager`` through
        ``INBOUND_FILE_CONTENT_KEY`` (same shape as Telegram).
        """
        del thread_id, user_id
        if not msg.files:
            return msg

        if self._relay:
            # [argus patch #171] The token here is the router key, not a Slack
            # token: it must never go to files.slack.com, and the router does
            # not proxy file downloads.
            names = [self._safe_inbound_filename(f.get("filename"), "attachment") for f in msg.files if isinstance(f, dict)]
            notice = f"[Slack attachment unavailable: {', '.join(names) or 'attachment'} (files are not relayed)]"
            msg.text = f"{msg.text}\n\n{notice}" if msg.text else notice
            msg.files = []
            return msg

        web_client = await self._get_web_client_for_inbound(msg)
        token = getattr(web_client, "token", None) if web_client is not None else None
        if not isinstance(token, str) or not token:
            notice = "[Slack attachment unavailable: bot token missing]"
            msg.text = f"{msg.text}\n\n{notice}" if msg.text else notice
            msg.files = []
            return msg

        materialized: list[dict[str, Any]] = []
        unavailable: list[str] = []
        materialized_bytes = 0

        for file_info in msg.files:
            if not isinstance(file_info, dict):
                continue

            filename = self._safe_inbound_filename(file_info.get("filename"), "attachment")
            declared_size = self._file_size(file_info.get("size"))
            if declared_size is not None and declared_size > SLACK_MAX_INBOUND_FILE_BYTES:
                logger.warning("[Slack] inbound file exceeds download limit, skipping: %s", filename)
                unavailable.append(f"{filename} (exceeds the download limit)")
                continue
            if declared_size is not None and materialized_bytes + declared_size > SLACK_MAX_INBOUND_MESSAGE_BYTES:
                logger.warning("[Slack] inbound message exceeds aggregate download limit, skipping: %s", filename)
                unavailable.append(f"{filename} (exceeds the message download limit)")
                continue

            url = file_info.get("url_private_download")
            if not isinstance(url, str) or not url.strip():
                logger.error("[Slack] cannot download inbound file without url_private_download: %s", filename)
                unavailable.append(f"{filename} (download unavailable)")
                continue

            url = url.strip()
            if not self._is_allowed_private_file_url(url):
                logger.warning("[Slack] inbound file URL host is not allowlisted, skipping: %s", filename)
                unavailable.append(f"{filename} (download unavailable)")
                continue

            remaining_budget = SLACK_MAX_INBOUND_MESSAGE_BYTES - materialized_bytes
            per_file_cap = min(SLACK_MAX_INBOUND_FILE_BYTES, remaining_budget)
            if per_file_cap <= 0:
                logger.warning("[Slack] inbound message aggregate download budget exhausted, skipping: %s", filename)
                unavailable.append(f"{filename} (exceeds the message download limit)")
                continue

            try:
                content = await self._download_private_file(url, token, max_bytes=per_file_cap)
            except Exception as exc:
                logger.error(
                    "[Slack] failed to download inbound file %s: %s",
                    filename,
                    type(exc).__name__,
                )
                unavailable.append(f"{filename} (download failed)")
                continue

            if content is None:
                # Stream aborted for size; encoding refusals raise above.
                unavailable.append(f"{filename} (exceeds the download limit)")
                continue

            if len(content) > SLACK_MAX_INBOUND_FILE_BYTES:
                logger.warning("[Slack] downloaded inbound file exceeds limit, skipping: %s", filename)
                unavailable.append(f"{filename} (exceeds the download limit)")
                continue
            if materialized_bytes + len(content) > SLACK_MAX_INBOUND_MESSAGE_BYTES:
                logger.warning("[Slack] downloaded inbound file exceeds aggregate message limit, skipping: %s", filename)
                unavailable.append(f"{filename} (exceeds the message download limit)")
                continue

            materialized.append(
                {
                    "type": "image" if file_info.get("type") == "image" else "file",
                    "filename": filename,
                    "mime_type": file_info.get("mime_type") if isinstance(file_info.get("mime_type"), str) else "application/octet-stream",
                    "size": len(content),
                    INBOUND_FILE_CONTENT_KEY: content,
                }
            )
            materialized_bytes += len(content)

        msg.files = materialized
        if unavailable:
            notice = f"[Slack attachment unavailable: {', '.join(unavailable)}]"
            msg.text = f"{msg.text}\n\n{notice}" if msg.text else notice
        return msg

    async def _get_web_client_for_inbound(self, msg: InboundMessage):
        """Resolve the WebClient whose token can fetch private Slack files."""
        return await self._web_client_for_connection(msg.connection_id)

    @classmethod
    def _is_allowed_private_file_url(cls, url: str) -> bool:
        """Gate Bearer downloads to https://*.files.slack.com (and the bare host)."""
        try:
            parsed = urlsplit(url)
        except ValueError:
            return False
        if parsed.scheme != "https":
            return False
        host = (parsed.hostname or "").lower()
        if not host:
            return False
        return any(host == suffix or host.endswith(f".{suffix}") for suffix in SLACK_ALLOWED_FILE_HOST_SUFFIXES)

    async def _download_private_file(self, url: str, token: str, *, max_bytes: int = SLACK_MAX_INBOUND_FILE_BYTES) -> bytes | None:
        """Stream a Slack private file URL with Bearer auth; never log the URL.

        Returns ``None`` only when the streamed body exceeds *max_bytes*.
        A non-identity ``Content-Encoding`` raises so callers can report
        ``(download failed)`` instead of a misleading size-limit notice.
        """
        chunks: list[bytes] = []
        total = 0
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as client:
            async with client.stream(
                "GET",
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept-Encoding": "identity",
                },
            ) as response:
                response.raise_for_status()
                encoding = (response.headers.get("content-encoding") or "").strip().lower()
                if encoding and encoding != "identity":
                    logger.warning(
                        "[Slack] inbound file response uses Content-Encoding %r, dropping before decode",
                        encoding,
                    )
                    raise RuntimeError(f"unsupported content-encoding: {encoding}")
                async for chunk in response.aiter_raw():
                    total += len(chunk)
                    if total > max_bytes:
                        logger.warning(
                            "[Slack] inbound file exceeds %d bytes download limit, dropping",
                            max_bytes,
                        )
                        return None
                    chunks.append(chunk)
        return b"".join(chunks)

    @classmethod
    def _extract_inbound_files(cls, event: dict) -> list[dict[str, Any]]:
        """Build token-free file descriptors from a Slack message event."""
        raw_files = event.get("files")
        if not isinstance(raw_files, list):
            return []

        files: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_files):
            if not isinstance(raw, dict):
                continue
            download_url = raw.get("url_private_download") or raw.get("url_private")
            if not isinstance(download_url, str) or not download_url.strip():
                continue
            file_id = raw.get("id")
            mime_type = raw.get("mimetype")
            if not isinstance(mime_type, str) or not mime_type:
                mime_type = "application/octet-stream"
            fallback = f"slack-file-{file_id if isinstance(file_id, str) and file_id else index}.bin"
            filename = cls._safe_inbound_filename(raw.get("name") or raw.get("title"), fallback)
            is_image = mime_type.startswith("image/") or str(raw.get("filetype") or "").lower() in {
                "png",
                "jpg",
                "jpeg",
                "gif",
                "webp",
                "bmp",
            }
            files.append(
                {
                    "type": "image" if is_image else "file",
                    "file_id": file_id if isinstance(file_id, str) else None,
                    "filename": filename,
                    "mime_type": mime_type,
                    "size": cls._file_size(raw.get("size")),
                    # Private URL only — Authorization is attached in receive_file.
                    "url_private_download": download_url.strip(),
                }
            )
        return files

    @staticmethod
    def _safe_inbound_filename(value: Any, fallback: str) -> str:
        if not isinstance(value, str):
            return fallback
        try:
            candidate = normalize_filename(value.strip())
        except (UnicodeError, ValueError):
            return fallback
        if is_upload_staging_file(candidate) or any(ord(char) < 32 for char in candidate):
            return fallback
        return candidate

    @staticmethod
    def _file_size(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if value >= 0 else None
        if isinstance(value, float) and value.is_integer() and value >= 0:
            return int(value)
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        return None

    async def _publish_inbound_with_connection(
        self,
        inbound,
        *,
        reservation: InboundReservation,
        team_id: str | None = None,
    ) -> None:
        try:
            inbound = await self._attach_connection_identity(inbound, team_id=team_id)
            self._commit_reserved_inbound(reservation, inbound)
        finally:
            reservation.release()

    async def _attach_connection_identity(self, inbound, *, team_id: str | None = None):
        workspace_id = str(team_id or inbound.metadata.get("team_id") or "")
        return await attach_connection_identity(
            inbound,
            repo=self._connection_repo,
            provider="slack",
            workspace_id=workspace_id,
        )

    async def _bind_connection_from_connect_code(self, *, event: dict, team_id: str, code: str) -> bool:
        if self._connection_repo is None or not code:
            return False

        channel_id = str(event.get("channel") or "")
        thread_ts = str(event.get("thread_ts") or event.get("ts") or "")
        state = await self._connection_repo.consume_oauth_state(provider="slack", state=code)
        if state is None:
            await self._post_connection_reply(channel_id, "Slack connection code is invalid or expired.", thread_ts)
            return True

        user_id = str(event.get("user") or "")
        if not user_id or not team_id:
            await self._post_connection_reply(channel_id, "Slack connection could not be completed from this message.", thread_ts)
            return True

        await self._connection_repo.upsert_connection(
            owner_user_id=state["owner_user_id"],
            provider="slack",
            external_account_id=user_id,
            workspace_id=team_id,
            metadata={
                "team_id": team_id,
                "channel_id": channel_id,
            },
            status="connected",
        )
        await self._post_connection_reply(channel_id, "Slack connected to DeerFlow.", thread_ts)
        return True

    async def _post_connection_reply(self, channel_id: str, text: str, thread_ts: str | None = None) -> None:
        if not self._web_client or not channel_id:
            return
        kwargs: dict[str, Any] = {"channel": channel_id, "text": text}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        try:
            await asyncio.to_thread(self._web_client.chat_postMessage, **kwargs)
        except Exception:
            logger.exception("[Slack] failed to send connection reply in channel=%s", channel_id)

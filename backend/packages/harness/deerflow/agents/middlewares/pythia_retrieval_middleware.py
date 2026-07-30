"""Deterministic company-knowledge retrieval before the model call.

Why this exists: a non-thinking Qwen lead agent does NOT reliably emit the
`pythia_query` tool call for company-knowledge questions — it answers from
memory and even confabulates "the Pythia MCP isn't accessible" (observed live).
You cannot make a model tool-call by asking harder. So this middleware takes
retrieval out of the model's hands: on the first model call of a turn it asks
the kb-api ROUTER what to fetch and injects the cited results, so the model
answers from authoritative context in a single shot. The agent's MCP tools stay
bound for going deeper.

Thin-client design (2026-06-02): routing + fetching now live server-side in
kb-api's `POST /{project}/answer` (src/routes/answer.py + src/pythia/router.py).
This middleware no longer classifies company-vs-other or calls pythia_query
itself — it makes ONE /answer call and injects whatever context_blocks come
back. An empty result means the router found no confident company-knowledge
route (Route.NONE / off-topic), so we inject nothing and let the agent proceed
on its own tools. All the routing logic (entity vs chunk vs recency vs none),
its tuning, and its tests are in kb-api, shared with the Slack @pythia bot and
every other consumer — not duplicated per stack.

Modeled on ViewImageMiddleware (same before_model inject pattern). Additive +
config-gated so it touches only stacks that opt in (Atlas).

Toggle / config (env, read once at construction):
  PYTHIA_ROUTER_INJECT       "1"/"true" to enable (default off — gated per stack)
                             (legacy alias: PYTHIA_RETRIEVAL_ENABLED)
  PYTHIA_KB_URL              base kb-api URL (default http://argus-kb-api:8000)
  PYTHIA_KB_PROJECT          KB project slug for company knowledge (default "pythia")
  KB_API_KEY                 kb-api key (already in the stack env)
  PYTHIA_RETRIEVAL_TIMEOUT   seconds before giving up + falling back (default 6)
  PYTHIA_RETRIEVAL_TOP_K     chunk hits to request (default 6)
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from typing import override

import httpx
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage

from deerflow.agents.thread_state import ThreadState

logger = logging.getLogger(__name__)

# Per-person rings require a gateway-signed caller token; external/internal
# do not. Mirrors kb-api pythia/rings.PRIVATE_RINGS.
_PRIVATE_RINGS = frozenset({"hierarchical", "personal"})


def _sign_caller(email: str, ttl_seconds: int = 120) -> str:
    """Mint a gateway-signed caller token kb-api will verify.

    CONTRACT (must stay byte-identical to kb-api pythia/caller_auth.py
    verify_caller): token = f"{email}.{exp}.{hexsig}", hexsig = HMAC-SHA256
    over f"{email}.{exp}" with key PYTHIA_CALLER_SIGNING_SECRET. The two
    sides are deliberately duplicated (kb-api module is not importable from
    the DeerFlow tree); the shared secret + this format are the coupling.
    Returns "" if no secret or no email (-> no token sent -> no private ring).
    """
    secret = os.environ.get("PYTHIA_CALLER_SIGNING_SECRET", "").encode()
    email = (email or "").strip().lower()
    if not secret or not email:
        return ""
    exp = int(time.time()) + ttl_seconds
    sig = hmac.new(secret, f"{email}.{exp}".encode(), hashlib.sha256).hexdigest()
    return f"{email}.{exp}.{sig}"

# Marker so we never re-inject within the same turn (mirrors ViewImage's guard).
_INJECT_MARKER = "[pythia-kb-context]"


class PythiaRetrievalMiddleware(AgentMiddleware[ThreadState]):
    """Inject kb-api router results before the model call (company questions)."""

    state_schema = ThreadState

    def __init__(self, ring: str = "none") -> None:
        super().__init__()
        # ring comes from the lead_agent gate, which reads the agent's
        # pythia_ring (config.yaml). The gate does not construct this middleware
        # for ring "none", so being constructed normally means retrieval is on.
        # ring is forwarded to kb-api as a CEILING (caller_ring); kb-api caps it
        # to what the verified caller may see and never escalates beyond it.
        #
        # [argus patch #48] Both defaults here are FAIL-CLOSED. They used to
        # default to "internal" and to enable on the stack flag alone, so
        # constructing this middleware with no ring — or with a ring string this
        # code does not recognise — silently granted internal-ring company-KB
        # retrieval. Combined with the old fail-open gate that turned an agent
        # declaring nothing into ring "internal", an agent whose config said
        # `pythia_ring: none` could still receive injected knowledge. Now an
        # unrecognised or absent ring retrieves NOTHING, and the env flag can
        # only disable, never enable.
        self.ring = (ring or "none").strip().lower()
        flag = (os.environ.get("PYTHIA_ROUTER_INJECT")
                or os.environ.get("PYTHIA_RETRIEVAL_ENABLED") or "").strip().lower()
        self.enabled = (self.ring in ("external", "internal", "hierarchical", "personal")
                        and flag not in ("0", "false", "no", "off"))
        self.base_url = os.environ.get("PYTHIA_KB_URL", "http://argus-kb-api:8000").rstrip("/")
        self.project = os.environ.get("PYTHIA_KB_PROJECT", "pythia")
        self.api_key = os.environ.get("KB_API_KEY", "")
        self.timeout = float(os.environ.get("PYTHIA_RETRIEVAL_TIMEOUT", "6"))
        self.top_k = int(os.environ.get("PYTHIA_RETRIEVAL_TOP_K", "6"))

    # --- turn/state helpers ------------------------------------------------

    def _latest_user_text(self, messages: list) -> str | None:
        for msg in reversed(messages):
            if isinstance(msg, HumanMessage):
                c = msg.content
                if isinstance(c, str):
                    return c
                if isinstance(c, list):  # multimodal: pull text blocks
                    return " ".join(b.get("text", "") for b in c
                                    if isinstance(b, dict) and b.get("type") == "text")
        return None

    def _already_handled_this_turn(self, messages: list) -> bool:
        """True if we should NOT inject for this model call. We inject only on the
        FIRST model call of a user turn.

        IMPORTANT (wrap_model_call vs before_model): wrap_model_call fires on
        EVERY model call in the agent loop (including tool-result follow-ups),
        whereas the old before_model fired once. We no longer persist an injected
        marker to state (the whole point of the move), so we can't detect "already
        injected" by scanning for _INJECT_MARKER in history. Instead: walk back
        from the end; if we hit an AIMessage before the latest HumanMessage, the
        model has already spoken this turn -> not the first call -> skip. We also
        still honor a marker if one somehow appears (belt-and-suspenders)."""
        for msg in reversed(messages):
            if isinstance(msg, HumanMessage):
                if _INJECT_MARKER in str(msg.content):
                    return True  # legacy/persisted inject present -> don't double
                return False  # reached the user turn, no assistant after it -> first call
            if isinstance(msg, AIMessage) and getattr(msg, "content", None):
                return True  # assistant already produced output this turn -> not first
        return False

    # --- routing + retrieval (delegated to kb-api /answer) -----------------

    def _route_and_fetch(self, query: str,
                         caller_email: str | None = None) -> tuple[dict, float, str | None]:
        """One call to the kb-api router. Returns (answer_json, elapsed_s,
        error). Never raises — a router/kb-api hiccup must not block the turn.

        For a private (personal/hierarchical) ring we mint a gateway-signed
        caller_token from the verified SSO email so kb-api can grant the
        owner-scoped ring. external/internal send no token (unchanged)."""
        url = f"{self.base_url}/{self.project}/answer"
        body = {"query": query, "top_k": self.top_k, "caller_ring": self.ring}
        if self.ring in _PRIVATE_RINGS and caller_email:
            token = _sign_caller(caller_email)
            if token:
                body["caller_token"] = token
                logger.info("[pythia-router] minted caller_token for %s (ring=%s)",
                            caller_email, self.ring)
            else:
                logger.warning("[pythia-router] ring=%s needs a token but signing "
                               "secret/email missing -> kb-api will clamp to internal",
                               self.ring)
        t = time.monotonic()
        try:
            r = httpx.post(
                url,
                headers={"X-Kb-Api-Key": self.api_key, "Content-Type": "application/json"},
                json=body,
                timeout=self.timeout,
            )
            r.raise_for_status()
            return r.json(), time.monotonic() - t, None
        except Exception as exc:  # noqa: BLE001 — degrade gracefully, never block
            return {}, time.monotonic() - t, repr(exc)

    def _format_context(self, query: str, answer: dict) -> str:
        """Render the router's context_blocks into an injected HumanMessage.
        Entity blocks carry structured facts; chunk blocks carry document text;
        both already include provenance."""
        blocks = answer.get("context_blocks", []) or []
        route = answer.get("route", "?")
        lines = [
            _INJECT_MARKER,
            "Authoritative results from the company knowledge base (Pythia) for "
            "the user's question. Answer FROM these and cite the source(s). Do "
            "not answer from memory or claim the KB is inaccessible — the results "
            f"are below (router route: {route}).",
            "",
        ]
        for i, b in enumerate(blocks, 1):
            prov = b.get("provenance", {}) or {}
            kind = b.get("kind", "result")
            title = b.get("title") or prov.get("source") or "result"
            date = prov.get("recency_date") or prov.get("event_date") or ""
            text = (b.get("text") or "").strip()
            hdr = f"[{i}] ({kind}) {title}"
            if date:
                hdr += f" | date: {date}"
            lines.append(hdr)
            lines.append(text)
            lines.append("")
        return "\n".join(lines)

    def _build_context_message(self, messages: list,
                               caller_email: str | None = None) -> HumanMessage | None:
        """Route+fetch for the current user turn and return a HumanMessage to
        inject into the MODEL REQUEST (not thread state), or None to inject
        nothing. Never raises."""
        if not self.enabled:
            return None
        if not messages:
            return None
        if self._already_handled_this_turn(messages):
            logger.info("[pythia-router] skip: not the first model call this turn")
            return None
        query = self._latest_user_text(messages)
        if not query:
            logger.info("[pythia-router] skip: no user text found")
            return None

        answer, elapsed, error = self._route_and_fetch(query, caller_email)
        if error:
            # A router/kb-api failure: stay silent and let the agent use its MCP
            # tools rather than injecting a "couldn't reach KB" message (the
            # tools remain the fallback path, so degradation is graceful).
            logger.info("[pythia-router] error, no inject: %s (%.0fms) q=%r",
                        error, elapsed * 1000.0, query[:120])
            return None

        blocks = answer.get("context_blocks", []) or []
        route = answer.get("route", "?")
        if not blocks:
            # Route.NONE / off-topic / empty: the router is not confident this is
            # a company-knowledge question, OR found nothing. Inject nothing; the
            # agent proceeds (web/personal tools, or declines). This is the Tier-3
            # design: never inject wrong/empty context.
            logger.info("[pythia-router] no context (route=%s) — no inject (%.0fms) q=%r",
                        route, elapsed * 1000.0, query[:120])
            return None

        logger.info("[pythia-router] fired: ring=%s route=%s blocks=%d conf=%.3f (%.0fms) q=%r",
                    self.ring, route, len(blocks), answer.get("confidence", 0.0),
                    elapsed * 1000.0, query[:120])
        # Injected as a HumanMessage (not SystemMessage): Qwen/vLLM rejects a
        # system message anywhere but the start. This message is appended to the
        # MODEL REQUEST only (see wrap_model_call), never to thread state, so it
        # is invisible on every surface (WebUI / Slack / Telegram / exports).
        return HumanMessage(content=self._format_context(query, answer))

    # --- model-call wrapping (NON-persisting injection) --------------------
    # We inject via wrap_model_call + request.override(), NOT before_model.
    # before_model returns a STATE UPDATE that gets committed to thread history
    # (which is why the [pythia-kb-context] block used to render in the thread).
    # request.override() is immutable and leaves thread state untouched: the KB
    # context reaches the model for THIS call only and is never persisted.

    @staticmethod
    def _caller_email(request) -> str | None:
        """The verified SSO email, stamped into runtime.context by the
        gateway (inject_authenticated_user_context). The authoritative,
        boundary-safe channel — see runtime/user_context.resolve_runtime_user_id."""
        ctx = getattr(getattr(request, "runtime", None), "context", None)
        if isinstance(ctx, dict):
            return ctx.get("user_email") or None
        return None

    def _inject_into_request(self, request):
        msg = self._build_context_message(request.messages, self._caller_email(request))
        if msg is None:
            return request
        return request.override(messages=[*request.messages, msg])

    @override
    def wrap_model_call(self, request, handler):
        return handler(self._inject_into_request(request))

    @override
    async def awrap_model_call(self, request, handler):
        # One bounded HTTP call (route+fetch, timeout-capped); acceptable to run
        # sync inside the async hook. Kept simple to avoid an async httpx client
        # lifecycle here.
        return await handler(self._inject_into_request(request))

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

import logging
import os
import time
from typing import override

import httpx
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.runtime import Runtime

from deerflow.agents.thread_state import ThreadState

logger = logging.getLogger(__name__)

# Marker so we never re-inject within the same turn (mirrors ViewImage's guard).
_INJECT_MARKER = "[pythia-kb-context]"


class PythiaRetrievalMiddleware(AgentMiddleware[ThreadState]):
    """Inject kb-api router results before the model call (company questions)."""

    state_schema = ThreadState

    def __init__(self) -> None:
        super().__init__()
        # PYTHIA_ROUTER_INJECT is the current flag; PYTHIA_RETRIEVAL_ENABLED is
        # the legacy alias kept so existing stack env files keep working.
        flag = (os.environ.get("PYTHIA_ROUTER_INJECT")
                or os.environ.get("PYTHIA_RETRIEVAL_ENABLED", ""))
        self.enabled = flag.lower() in ("1", "true", "yes")
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
        """True if we've already injected for the current user turn, OR the
        model has already produced output this turn (we only act on the FIRST
        model call of a turn — before any assistant message exists for it)."""
        for msg in reversed(messages):
            if isinstance(msg, HumanMessage):
                if _INJECT_MARKER in str(msg.content):
                    return True
                return False  # reached the user turn with no injection yet
            if isinstance(msg, AIMessage) and getattr(msg, "content", None):
                continue  # an assistant turn already happened -> not first call
        return False

    # --- routing + retrieval (delegated to kb-api /answer) -----------------

    def _route_and_fetch(self, query: str) -> tuple[dict, float, str | None]:
        """One call to the kb-api router. Returns (answer_json, elapsed_s,
        error). Never raises — a router/kb-api hiccup must not block the turn."""
        url = f"{self.base_url}/{self.project}/answer"
        t = time.monotonic()
        try:
            r = httpx.post(
                url,
                headers={"X-Kb-Api-Key": self.api_key, "Content-Type": "application/json"},
                json={"query": query, "top_k": self.top_k},
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

    def _maybe_inject(self, state: ThreadState) -> dict | None:
        if not self.enabled:
            return None
        messages = state.get("messages", []) or []
        if not messages:
            return None
        if self._already_handled_this_turn(messages):
            logger.info("[pythia-router] skip: already handled this turn")
            return None
        query = self._latest_user_text(messages)
        if not query:
            logger.info("[pythia-router] skip: no user text found")
            return None

        answer, elapsed, error = self._route_and_fetch(query)
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

        logger.info("[pythia-router] fired: route=%s blocks=%d conf=%.3f (%.0fms) q=%r",
                    route, len(blocks), answer.get("confidence", 0.0),
                    elapsed * 1000.0, query[:120])
        # Inject as a HumanMessage, NOT a SystemMessage: appended AFTER the
        # user's message, and Qwen/vLLM rejects a system message anywhere but the
        # start. ViewImageMiddleware injects a HumanMessage for the same reason.
        return {"messages": [HumanMessage(content=self._format_context(query, answer))]}

    @override
    def before_model(self, state: ThreadState, runtime: Runtime) -> dict | None:
        return self._maybe_inject(state)

    @override
    async def abefore_model(self, state: ThreadState, runtime: Runtime) -> dict | None:
        # One bounded HTTP call (route+fetch, timeout-capped); acceptable to run
        # sync inside the async hook. Kept simple to avoid an async httpx client
        # lifecycle here.
        return self._maybe_inject(state)

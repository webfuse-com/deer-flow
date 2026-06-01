"""Deterministic company-knowledge retrieval before the model call.

Why this exists: a non-thinking Qwen lead agent does NOT reliably emit the
`pythia_query` tool call for company-knowledge questions — it answers from
memory and even confabulates "the Pythia MCP isn't accessible" (observed live).
Forceful SOUL/skill instructions did not fix it; you cannot make a model
tool-call by asking harder. So this middleware takes retrieval out of the
model's hands: on the first model call of a turn, if the user's question is a
company-knowledge question, it calls Pythia ITSELF and injects the cited
results into the conversation, so the model answers from authoritative context.
Works in fast non-thinking mode and cannot confabulate "no access".

Modeled on ViewImageMiddleware (same before_model inject pattern). Additive +
config-gated so it touches only stacks that opt in (Atlas).

Toggle / config (env, read once at construction):
  PYTHIA_RETRIEVAL_ENABLED   "1"/"true" to enable (default off — gated per stack)
  PYTHIA_KB_URL              base kb-api URL (default http://argus-kb-api:8000)
  PYTHIA_KB_PROJECT          KB project slug for company knowledge (default "pythia")
  KB_API_KEY                 kb-api key (already in the stack env)
  PYTHIA_RETRIEVAL_TIMEOUT   seconds before giving up + falling back (default 5)
  PYTHIA_RETRIEVAL_TOP_K     hits to inject (default 5)
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import override

import httpx
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.runtime import Runtime

from deerflow.agents.thread_state import ThreadState

logger = logging.getLogger(__name__)

# Marker so we never re-inject within the same turn (mirrors ViewImage's guard).
_INJECT_MARKER = "[pythia-kb-context]"

# Company-knowledge intent: keyword/heuristic v1, deliberately biased to FIRE
# (a false-positive retrieval costs ~0.2s; a missed one is the bug we're fixing).
# A later upgrade can swap this for the embedding-centroid classifier in
# kb-api/src/pythia/intent.py. These cover meetings, policies, customers,
# contracts, decisions, ownership — the company-record surface.
_COMPANY_PATTERNS = re.compile(
    r"\b("
    r"campfire|pitwall|all[- ]?hands|minutes|meeting|stand[- ]?up|"
    r"polic(y|ies)|procedure|retention|isms|p3p|security policy|onboarding|offboarding|"
    r"contract|renewal|mrr|vendor|customer|account|subscription|chargebee|"
    r"dri|who owns|who is responsible|what did we (decide|agree)|decision|roadmap|"
    r"confluence|wiki|spec|charter"
    r")\b",
    re.IGNORECASE,
)


def _looks_like_company_question(text: str) -> bool:
    return bool(text) and bool(_COMPANY_PATTERNS.search(text))


class PythiaRetrievalMiddleware(AgentMiddleware[ThreadState]):
    """Inject Pythia KB results before the model call for company questions."""

    state_schema = ThreadState

    def __init__(self) -> None:
        super().__init__()
        self.enabled = os.environ.get("PYTHIA_RETRIEVAL_ENABLED", "").lower() in ("1", "true", "yes")
        self.base_url = os.environ.get("PYTHIA_KB_URL", "http://argus-kb-api:8000").rstrip("/")
        self.project = os.environ.get("PYTHIA_KB_PROJECT", "pythia")
        self.api_key = os.environ.get("KB_API_KEY", "")
        self.timeout = float(os.environ.get("PYTHIA_RETRIEVAL_TIMEOUT", "5"))
        self.top_k = int(os.environ.get("PYTHIA_RETRIEVAL_TOP_K", "5"))

    # --- turn/state helpers ------------------------------------------------

    def _latest_user_text(self, messages: list) -> str | None:
        for msg in reversed(messages):
            if isinstance(msg, HumanMessage):
                c = msg.content
                if isinstance(c, str):
                    return c
                if isinstance(c, list):  # multimodal: pull text blocks
                    return " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
        return None

    def _already_handled_this_turn(self, messages: list) -> bool:
        """True if we've already injected for the current user turn, OR the
        model has already produced output this turn (we only act on the FIRST
        model call of a turn — before any assistant message exists for it)."""
        # Walk back to the latest human message; if anything after it is our
        # injected marker OR an assistant message, we've passed the inject point.
        for msg in reversed(messages):
            if isinstance(msg, HumanMessage):
                content = str(msg.content)
                if _INJECT_MARKER in content:
                    return True
                return False  # reached the user turn with no injection yet
            if isinstance(msg, (AIMessage,)) and getattr(msg, "content", None):
                # an assistant turn already happened after the user msg -> not first call
                continue
        return False

    # --- retrieval ---------------------------------------------------------

    def _retrieve(self, query: str) -> tuple[list[dict], float, str | None]:
        """Call Pythia. Returns (hits, elapsed_s, error). Never raises."""
        url = f"{self.base_url}/{self.project}/pythia/query"
        t = time.monotonic()
        try:
            r = httpx.post(
                url,
                headers={"X-Kb-Api-Key": self.api_key, "Content-Type": "application/json"},
                json={"query": query, "top_k": self.top_k},
                timeout=self.timeout,
            )
            r.raise_for_status()
            return r.json().get("hits", []), time.monotonic() - t, None
        except Exception as exc:  # noqa: BLE001 — degrade gracefully, never block the turn
            return [], time.monotonic() - t, repr(exc)

    def _format_context(self, query: str, hits: list[dict], error: str | None) -> str:
        if error:
            return (
                f"{_INJECT_MARKER}\nThe company knowledge base (Pythia) could not be reached "
                f"for this question ({error}). Tell the user you couldn't retrieve the company "
                f"record right now; do not invent an answer."
            )
        if not hits:
            return (
                f"{_INJECT_MARKER}\nThe company knowledge base (Pythia) returned no results for: "
                f"\"{query}\". Say plainly that the KB has nothing on this; do not answer from memory."
            )
        lines = [
            f"{_INJECT_MARKER}",
            "Authoritative results from the company knowledge base (Pythia) for the user's question. "
            "Answer FROM these, and cite the source(s). Do not answer from memory or claim the KB is "
            "inaccessible — the results are below.",
            "",
        ]
        for i, h in enumerate(hits, 1):
            prov = h.get("provenance", {}) or {}
            src = prov.get("source") or prov.get("source_detail") or "unknown source"
            date = prov.get("recency_date") or prov.get("event_date") or ""
            content = (h.get("content") or "").strip()
            lines.append(f"[{i}] source: {src}{(' | date: ' + date) if date else ''}")
            lines.append(content)
            lines.append("")
        return "\n".join(lines)

    def _maybe_inject(self, state: ThreadState) -> dict | None:
        if not self.enabled:
            return None
        messages = state.get("messages", []) or []
        if not messages or self._already_handled_this_turn(messages):
            return None
        query = self._latest_user_text(messages)
        if not query or not _looks_like_company_question(query):
            logger.debug("[pythia-retrieval] skip: not a company question")
            return None

        hits, elapsed, error = self._retrieve(query)
        logger.info(
            "[pythia-retrieval] fired: hits=%d elapsed=%.0fms%s",
            len(hits), elapsed * 1000.0, f" error={error}" if error else "",
        )
        return {"messages": [SystemMessage(content=self._format_context(query, hits, error))]}

    @override
    def before_model(self, state: ThreadState, runtime: Runtime) -> dict | None:
        return self._maybe_inject(state)

    @override
    async def abefore_model(self, state: ThreadState, runtime: Runtime) -> dict | None:
        # Retrieval is a short bounded HTTP call; running it sync inside the async
        # hook is acceptable at ~0.2s typical / timeout-capped. Kept simple to
        # avoid an async httpx client lifecycle here.
        return self._maybe_inject(state)

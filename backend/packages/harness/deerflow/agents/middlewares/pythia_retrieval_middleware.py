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
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.runtime import Runtime

from deerflow.agents.thread_state import ThreadState

logger = logging.getLogger(__name__)

# Marker so we never re-inject within the same turn (mirrors ViewImage's guard).
_INJECT_MARKER = "[pythia-kb-context]"

# Company-knowledge intent — KEYWORD fallback (used only if the embedding
# classifier can't reach the embedder). Biased to fire. Kept narrow on purpose;
# the semantic classifier below is the primary path and catches phrasings the
# keywords miss (e.g. "Code of Conduct", "holiday allowance").
_COMPANY_PATTERNS = re.compile(
    r"\b("
    r"campfire|pitwall|all[- ]?hands|minutes|meeting|stand[- ]?up|"
    r"polic(y|ies)|procedure|retention|isms|p3p|security policy|onboarding|offboarding|"
    r"code of conduct|conduct|handbook|hr|benefits|pto|holiday|expense|"
    r"contract|renewal|mrr|vendor|customer|account|subscription|chargebee|"
    r"dri|who owns|who is responsible|what did we (decide|agree)|decision|roadmap|"
    r"confluence|wiki|spec|charter"
    r")\b",
    re.IGNORECASE,
)


def _keyword_is_company(text: str) -> bool:
    return bool(text) and bool(_COMPANY_PATTERNS.search(text))


# --- semantic (embedding-centroid) classifier -----------------------------
# Primary classifier: embed the question, compare cosine to a COMPANY-knowledge
# centroid vs a PERSONAL/OTHER centroid (each averaged from example phrasings).
# Fire if company wins by a margin. Matches by MEANING, so "Code of Conduct",
# "what's our holiday allowance", etc. land near the company centroid without a
# literal keyword. Same approach as kb-api/src/pythia/intent.py; reimplemented
# here (dependency-free) because that module isn't importable in this container.

_COMPANY_EXAMPLES = [
    "What was discussed at the last Campfire?",
    "What did we decide at the Pitwall?",
    "What is our data retention policy?",
    "What does our Code of Conduct say?",
    "What's our expense / travel policy?",
    "What is our holiday / PTO allowance?",
    "What are the company onboarding steps?",
    "What's the status of our contract with this customer?",
    "What's the MRR for this account?",
    "Who is the DRI for the website?",
    "Who owns this area / responsibility?",
    "What did we agree about pricing?",
    "What's in the security / ISMS policy?",
    "Summarize the latest company all-hands.",
    "What's our incident response procedure?",
]
_OTHER_EXAMPLES = [
    "Summarize my inbox.",
    "Draft a reply to this email.",
    "What's on my calendar today?",
    "Write a python function to sort a list.",
    "Help me debug this stack trace.",
    "What's the weather in Amsterdam?",
    "Translate this paragraph to Dutch.",
    "Refactor this code for readability.",
    "Set a reminder for tomorrow.",
    "What is the capital of France?",
]


class _SemanticCompanyClassifier:
    """Lazily-built centroid classifier over the LiteLLM embedding endpoint."""

    def __init__(self, base_url: str, model: str, api_key: str, margin: float = 0.02):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.margin = margin
        self._company_centroid: list[float] | None = None
        self._other_centroid: list[float] | None = None

    def _embed(self, texts: list[str]) -> list[list[float]]:
        r = httpx.post(
            f"{self.base_url}/embeddings",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={"model": self.model, "input": texts, "encoding_format": "float"},
            timeout=20.0,
        )
        r.raise_for_status()
        data = r.json()["data"]
        by_idx = {d["index"]: d["embedding"] for d in data}
        return [by_idx[i] for i in range(len(texts))]

    @staticmethod
    def _centroid(vecs: list[list[float]]) -> list[float]:
        n, dim = len(vecs), len(vecs[0])
        return [sum(v[i] for v in vecs) / n for i in range(dim)]

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(y * y for y in b) ** 0.5
        return dot / (na * nb) if na and nb else 0.0

    def _ensure_centroids(self) -> None:
        if self._company_centroid is not None:
            return
        all_vecs = self._embed(_COMPANY_EXAMPLES + _OTHER_EXAMPLES)
        nc = len(_COMPANY_EXAMPLES)
        self._company_centroid = self._centroid(all_vecs[:nc])
        self._other_centroid = self._centroid(all_vecs[nc:])

    def is_company(self, text: str) -> tuple[bool, float]:
        """Returns (is_company, company_minus_other_cosine). Raises on embed error."""
        self._ensure_centroids()
        (qv,) = self._embed([text])
        cs = self._cosine(qv, self._company_centroid)
        os_ = self._cosine(qv, self._other_centroid)
        return (cs - os_) >= self.margin, cs - os_


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
        # Semantic classifier over the LiteLLM embedding endpoint (primary).
        litellm_url = os.environ.get("LITELLM_BASE_URL", "http://argus-litellm:4000/v1")
        embed_model = os.environ.get("EMBEDDING_MODEL", "embedding")
        litellm_key = os.environ.get("LITELLM_MASTER_KEY", "")
        self._classifier = _SemanticCompanyClassifier(litellm_url, embed_model, litellm_key)

    def _is_company_question(self, text: str) -> bool:
        """Semantic classify; fall back to keywords if the embedder is unreachable."""
        try:
            ok, score = self._classifier.is_company(text)
            logger.info("[pythia-retrieval] classify(semantic): company=%s score=%.3f q=%r",
                        ok, score, text[:120])
            return ok
        except Exception as exc:  # noqa: BLE001 — degrade to keywords, never block
            ok = _keyword_is_company(text)
            logger.info("[pythia-retrieval] classify(keyword-fallback, embed err=%r): company=%s q=%r",
                        exc, ok, text[:120])
            return ok

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
        if not messages:
            return None
        if self._already_handled_this_turn(messages):
            logger.info("[pythia-retrieval] skip: already handled this turn")
            return None
        query = self._latest_user_text(messages)
        if not query:
            logger.info("[pythia-retrieval] skip: no user text found")
            return None
        if not self._is_company_question(query):
            return None  # _is_company_question already logged the decision

        hits, elapsed, error = self._retrieve(query)
        logger.info(
            "[pythia-retrieval] fired: hits=%d elapsed=%.0fms%s",
            len(hits), elapsed * 1000.0, f" error={error}" if error else "",
        )
        # Inject as a HumanMessage, NOT a SystemMessage: this is appended AFTER
        # the user's message, and Qwen/vLLM rejects a system message anywhere but
        # the start ("System message must be at the beginning"). ViewImageMiddleware
        # injects a HumanMessage for the same reason.
        return {"messages": [HumanMessage(content=self._format_context(query, hits, error))]}

    @override
    def before_model(self, state: ThreadState, runtime: Runtime) -> dict | None:
        return self._maybe_inject(state)

    @override
    async def abefore_model(self, state: ThreadState, runtime: Runtime) -> dict | None:
        # Retrieval is a short bounded HTTP call; running it sync inside the async
        # hook is acceptable at ~0.2s typical / timeout-capped. Kept simple to
        # avoid an async httpx client lifecycle here.
        return self._maybe_inject(state)

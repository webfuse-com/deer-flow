"""[argus] Tests for PythiaRetrievalMiddleware (deterministic company-KB retrieval).

Covers the ring/enable gating, the gateway-signed caller-token contract (which
must stay byte-identical to kb-api's verifier), context formatting + the
per-turn dedup marker, and the lead-agent wiring that decides whether the
middleware is attached at all.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from deerflow.agents.middlewares.pythia_retrieval_middleware import (
    _INJECT_MARKER,
    PythiaRetrievalMiddleware,
    _sign_caller,
)


class TestRingGating:
    def test_internal_ring_is_enabled(self, monkeypatch):
        monkeypatch.delenv("PYTHIA_ROUTER_INJECT", raising=False)
        monkeypatch.delenv("PYTHIA_RETRIEVAL_ENABLED", raising=False)
        mw = PythiaRetrievalMiddleware(ring="internal")
        assert mw.ring == "internal"
        assert mw.enabled is True

    def test_external_ring_is_enabled(self, monkeypatch):
        monkeypatch.delenv("PYTHIA_ROUTER_INJECT", raising=False)
        monkeypatch.delenv("PYTHIA_RETRIEVAL_ENABLED", raising=False)
        assert PythiaRetrievalMiddleware(ring="external").enabled is True

    def test_ring_is_lowercased_and_stripped(self):
        assert PythiaRetrievalMiddleware(ring="  Internal  ").ring == "internal"

    def test_stack_flag_enables_even_unknown_ring(self, monkeypatch):
        monkeypatch.setenv("PYTHIA_ROUTER_INJECT", "1")
        # A non-retrieving ring string still ends up enabled via the legacy flag.
        assert PythiaRetrievalMiddleware(ring="bogus").enabled is True


class TestCallerTokenContract:
    """The token format is duplicated in kb-api; it must stay byte-identical."""

    def test_no_secret_yields_empty_token(self, monkeypatch):
        monkeypatch.delenv("PYTHIA_CALLER_SIGNING_SECRET", raising=False)
        assert _sign_caller("alice@surfly.com") == ""

    def test_no_email_yields_empty_token(self, monkeypatch):
        monkeypatch.setenv("PYTHIA_CALLER_SIGNING_SECRET", "s3cr3t")
        assert _sign_caller("") == ""

    def test_token_format_matches_kb_api_verifier(self, monkeypatch):
        monkeypatch.setenv("PYTHIA_CALLER_SIGNING_SECRET", "s3cr3t")
        token = _sign_caller("Alice@Surfly.com", ttl_seconds=120)
        # token = "{email}.{exp}.{hexsig}"; the email itself contains dots
        # (surfly.com), so the verifier rsplits from the right.
        email, exp, sig = token.rsplit(".", 2)
        assert email == "alice@surfly.com"
        expected = hmac.new(b"s3cr3t", f"{email}.{exp}".encode(), hashlib.sha256).hexdigest()
        assert sig == expected


class TestContextFormatting:
    def test_format_context_marks_and_cites(self):
        mw = PythiaRetrievalMiddleware(ring="internal")
        answer = {
            "route": "company",
            "context_blocks": [
                {"kind": "entity", "title": "Surfly", "text": "Co-browsing company.", "provenance": {"source": "crm", "recency_date": "2026-06-01"}},
            ],
        }
        rendered = mw._format_context("what is surfly", answer)
        assert rendered.startswith(_INJECT_MARKER)
        assert "Surfly" in rendered
        assert "Co-browsing company." in rendered
        assert "2026-06-01" in rendered
        assert "router route: company" in rendered

    def test_already_handled_guard_detects_marker(self):
        mw = PythiaRetrievalMiddleware(ring="internal")
        messages = [HumanMessage(content="q"), HumanMessage(content=f"{_INJECT_MARKER}\nstuff")]
        assert mw._already_handled_this_turn(messages) is True

    def test_first_call_of_turn_is_not_handled(self):
        """A fresh user turn (latest message is the human, no assistant after
        it) is the first model call -> inject."""
        mw = PythiaRetrievalMiddleware(ring="internal")
        messages = [AIMessage(content="prior answer"), HumanMessage(content="new question")]
        assert mw._already_handled_this_turn(messages) is False

    def test_tool_followup_call_is_handled(self):
        """Once the assistant has spoken this turn (e.g. a tool-result
        follow-up call), do NOT re-inject."""
        mw = PythiaRetrievalMiddleware(ring="internal")
        messages = [HumanMessage(content="q"), AIMessage(content="working on it")]
        assert mw._already_handled_this_turn(messages) is True

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

# Sentinel for "the agent has no config entry at all" (the synthetic "default").
_MISSING = object()


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

    # --- patch #48: fail-closed ------------------------------------------
    # These four replace test_stack_flag_enables_even_unknown_ring, which
    # asserted the OLD fail-open contract (the stack flag enabling retrieval for
    # any ring string, including unrecognised ones).

    def test_unknown_ring_retrieves_nothing_even_with_the_flag_on(self, monkeypatch):
        monkeypatch.setenv("PYTHIA_ROUTER_INJECT", "1")
        assert PythiaRetrievalMiddleware(ring="bogus").enabled is False

    def test_default_ring_is_none_and_disabled(self, monkeypatch):
        # Constructing with no ring must not grant internal-ring retrieval.
        monkeypatch.setenv("PYTHIA_ROUTER_INJECT", "1")
        mw = PythiaRetrievalMiddleware()
        assert mw.ring == "none"
        assert mw.enabled is False

    def test_empty_ring_is_disabled(self, monkeypatch):
        monkeypatch.setenv("PYTHIA_ROUTER_INJECT", "1")
        assert PythiaRetrievalMiddleware(ring="").enabled is False

    @pytest.mark.parametrize("flag", ["0", "false", "no", "off", "FALSE", " off "])
    def test_stack_flag_is_a_kill_switch(self, monkeypatch, flag):
        # A false flag disables retrieval even for an agent that opts in.
        monkeypatch.setenv("PYTHIA_ROUTER_INJECT", flag)
        assert PythiaRetrievalMiddleware(ring="internal").enabled is False

    def test_legacy_alias_also_kills(self, monkeypatch):
        monkeypatch.delenv("PYTHIA_ROUTER_INJECT", raising=False)
        monkeypatch.setenv("PYTHIA_RETRIEVAL_ENABLED", "0")
        assert PythiaRetrievalMiddleware(ring="internal").enabled is False


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


class TestLeadAgentWiring:
    """[argus patch #48] Whether the middleware is ATTACHED AT ALL.

    This class is the regression net for the bug that made #48 necessary. The
    module docstring already claimed to cover "the lead-agent wiring that decides
    whether the middleware is attached at all" — it did not, and that is exactly
    where the fail-open default lived: an agent declaring no pythia_ring
    inherited ring "internal" from the stack flag. A turn that names no agent
    runs as the synthetic "default" agent, which has no entry in agents/, so
    every such turn silently received internal-ring company-KB injection.
    """

    @staticmethod
    def _rings(monkeypatch, agent_ring, *, agent_name="atlas", flag=None):
        """Build the chain and report whether PythiaRetrievalMiddleware attached."""
        from deerflow.agents.lead_agent import agent as lead_agent_module
        from deerflow.config.agents_config import AgentConfig
        from deerflow.config.app_config import AppConfig
        from deerflow.config.model_config import ModelConfig
        from deerflow.config.sandbox_config import SandboxConfig

        if flag is None:
            monkeypatch.delenv("PYTHIA_ROUTER_INJECT", raising=False)
            monkeypatch.delenv("PYTHIA_RETRIEVAL_ENABLED", raising=False)
        else:
            monkeypatch.setenv("PYTHIA_ROUTER_INJECT", flag)

        # `agent_ring is _MISSING` models the synthetic "default" agent: no
        # config entry at all, so load_agent_config raises and _agent_config
        # stays None.
        if agent_ring is _MISSING:
            monkeypatch.setattr(lead_agent_module, "load_agent_config", lambda name: (_ for _ in ()).throw(KeyError(name)))
        else:
            cfg = AgentConfig(name=agent_name, pythia_ring=agent_ring)
            monkeypatch.setattr(lead_agent_module, "load_agent_config", lambda name: cfg)

        app_config = AppConfig(
            models=[
                ModelConfig(
                    name="m",
                    display_name="m",
                    description=None,
                    use="langchain_openai:ChatOpenAI",
                    model="m",
                    supports_thinking=False,
                    supports_vision=False,
                )
            ],
            sandbox=SandboxConfig(use="deerflow.sandbox.local:LocalSandboxProvider"),
        )
        chain = lead_agent_module.build_middlewares(
            {"configurable": {"is_plan_mode": False, "subagent_enabled": False}},
            model_name="m",
            agent_name=agent_name,
            app_config=app_config,
        )
        found = [m for m in chain if isinstance(m, PythiaRetrievalMiddleware)]
        return found[0].ring if found else None

    def test_agent_declaring_nothing_gets_no_retrieval(self, monkeypatch):
        """THE BUG: this returned "internal" before #48 whenever the stack flag
        was on, which it is on every argus stack."""
        assert self._rings(monkeypatch, None, flag="1") is None

    def test_synthetic_default_agent_gets_no_retrieval(self, monkeypatch):
        """The exact production path: a turn naming no agent resolves to
        "default", which has no config entry."""
        assert self._rings(monkeypatch, _MISSING, agent_name="default", flag="1") is None

    def test_explicit_none_gets_no_retrieval(self, monkeypatch):
        assert self._rings(monkeypatch, "none", flag="1") is None

    def test_opt_in_internal_attaches(self, monkeypatch):
        assert self._rings(monkeypatch, "internal", flag="1") == "internal"

    def test_opt_in_works_without_the_stack_flag(self, monkeypatch):
        """Opting in is sufficient; the flag is no longer an enabler."""
        assert self._rings(monkeypatch, "internal", flag=None) == "internal"

    def test_opt_in_external_attaches(self, monkeypatch):
        assert self._rings(monkeypatch, "external", flag=None) == "external"

    def test_false_flag_kills_an_opted_in_agent(self, monkeypatch):
        assert self._rings(monkeypatch, "internal", flag="0") is None

    def test_ring_is_normalised(self, monkeypatch):
        assert self._rings(monkeypatch, "  Internal  ", flag=None) == "internal"

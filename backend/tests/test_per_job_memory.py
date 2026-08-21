"""Tests for [argus patch #30] per-job memory policy (§4b).

A scheduled (unattended) turn must be read-only against the citizen's long-term
memory by default: same agent, same thread, same memory.json, but a job turn
queues NO memory update. `memory: read-write` opts a job back into writing;
`memory: off` additionally suppresses memory injection. Interactive turns
(no memory_mode on runtime.context) are unaffected.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from langchain_core.messages import AIMessage, HumanMessage

from deerflow.agents.middlewares.dynamic_context_middleware import DynamicContextMiddleware
from deerflow.agents.middlewares.memory_middleware import MemoryMiddleware


def _runtime(context: dict | None = None):
    return SimpleNamespace(context=context or {})


def _conversation_state():
    # One human + one AI message — the minimum MemoryMiddleware enqueues on.
    return {"messages": [HumanMessage(content="hi", id="m1"), AIMessage(content="hello", id="m2")]}


def _run_after_agent(context: dict):
    """Run MemoryMiddleware.after_agent with a patched manager; return the manager mock."""
    mw = MemoryMiddleware(agent_name="atlas")
    state = _conversation_state()
    manager = mock.MagicMock()
    with (
        mock.patch("deerflow.agents.middlewares.memory_middleware.get_memory_manager", return_value=manager),
        mock.patch("deerflow.agents.middlewares.memory_middleware.get_memory_config", return_value=SimpleNamespace(enabled=True)),
        mock.patch("deerflow.agents.middlewares.memory_middleware.resolve_runtime_user_id", return_value="default"),
    ):
        mw.after_agent(state, _runtime({"thread_id": "t1", **context}))
    return manager


class TestMemoryWriteGate:
    def test_interactive_turn_writes(self):
        """No memory_mode / not unattended → normal write (regression guard)."""
        manager = _run_after_agent({})
        manager.add.assert_called_once()

    def test_unattended_default_is_read_only(self):
        """unattended with no explicit mode → read-only → NO write."""
        manager = _run_after_agent({"unattended": True})
        manager.add.assert_not_called()

    def test_read_only_does_not_write(self):
        manager = _run_after_agent({"unattended": True, "memory_mode": "read-only"})
        manager.add.assert_not_called()

    def test_off_does_not_write(self):
        manager = _run_after_agent({"unattended": True, "memory_mode": "off"})
        manager.add.assert_not_called()

    def test_read_write_writes(self):
        """A job that explicitly opts into read-write DOES queue a write."""
        manager = _run_after_agent({"unattended": True, "memory_mode": "read-write"})
        manager.add.assert_called_once()


class TestMemoryInjectionGate:
    def _inject(self, context: dict) -> str:
        mw = DynamicContextMiddleware(agent_name="atlas")
        state = {"messages": [HumanMessage(content="Hello", id="msg-1")]}
        with mock.patch("deerflow.agents.lead_agent.prompt._get_memory_context", return_value="<memory>secret fact</memory>"), mock.patch("deerflow.agents.middlewares.dynamic_context_middleware.datetime") as mock_dt:
            mock_dt.now.return_value.strftime.return_value = "2026-06-29, Monday"
            result = mw.before_agent(state, _runtime(context))
        if not result:
            return ""
        # The reminder now spans separate messages (date SystemMessage, hidden
        # memory HumanMessage, user turn); the policy is about what got
        # injected anywhere in the update.
        return "\n".join(m.content for m in result["messages"] if isinstance(m.content, str))

    def test_read_only_still_injects_memory(self):
        """read-only reads memory (injects) — it just doesn't write."""
        content = self._inject({"unattended": True, "memory_mode": "read-only"})
        assert "<memory>secret fact</memory>" in content

    def test_off_suppresses_memory_injection(self):
        """off neither reads nor writes — no memory in the reminder, date still present."""
        content = self._inject({"unattended": True, "memory_mode": "off"})
        assert "secret fact" not in content
        assert "<current_date>" in content

    def test_interactive_injects_memory(self):
        content = self._inject({})
        assert "<memory>secret fact</memory>" in content


class TestSummarizationFlushGate:
    """The pre-compression flush is a SECOND memory-write path; it must honor the
    same policy or an unattended turn that summarizes would still write."""

    def _flush(self, context: dict):
        from types import SimpleNamespace

        from deerflow.agents.memory import summarization_hook as sh

        event = SimpleNamespace(
            thread_id="t1",
            agent_name="atlas",
            messages_to_summarize=[HumanMessage(content="hi", id="m1"), AIMessage(content="hello", id="m2")],
            runtime=_runtime(context),
        )
        manager = mock.MagicMock()
        with (
            mock.patch.object(sh, "get_memory_config", return_value=SimpleNamespace(enabled=True)),
            mock.patch.object(sh, "get_memory_manager", return_value=manager),
            mock.patch.object(sh, "resolve_runtime_user_id", return_value="default"),
        ):
            sh.memory_flush_hook(event)
        return manager

    def test_unattended_flush_does_not_write(self):
        manager = self._flush({"unattended": True, "memory_mode": "read-only"})
        manager.add_nowait.assert_not_called()

    def test_off_flush_does_not_write(self):
        manager = self._flush({"unattended": True, "memory_mode": "off"})
        manager.add_nowait.assert_not_called()

    def test_interactive_flush_writes(self):
        manager = self._flush({})
        manager.add_nowait.assert_called_once()

    def test_read_write_flush_writes(self):
        manager = self._flush({"unattended": True, "memory_mode": "read-write"})
        manager.add_nowait.assert_called_once()

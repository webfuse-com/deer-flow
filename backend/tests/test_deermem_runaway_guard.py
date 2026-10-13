"""[argus patch #135] DeerMem memory-update runaway guards.

atlas-ashish 2026-10-12 21:51 CEST: one update reasoned for 1,540 s, emitted
131,072 completion tokens, failed to parse and was lost; the serial queue held
the next update for 25 minutes. Caps on the call, and a batch that keeps
failing is skipped instead of re-fed on every later turn.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from deerflow.agents.memory.backends.deermem.deermem.config import DeerMemConfig, DeerMemModelConfig
from deerflow.agents.memory.backends.deermem.deermem.core import llm as llm_module
from deerflow.agents.memory.backends.deermem.deermem.core.storage import MemoryStorage
from deerflow.agents.memory.backends.deermem.deermem.core.updater import MemoryUpdater

_EMPTY = json.dumps({"user": {}, "history": {}, "newFacts": [], "factsToRemove": [], "staleFactsToRemove": [], "staleFactsToExtend": [], "factsToConsolidate": []})


class _Response:
    def __init__(self, content: str, finish_reason: str | None = None) -> None:
        self.content = content
        self.usage_metadata = None
        self.response_metadata = {"finish_reason": finish_reason} if finish_reason else {}


class _ScriptedLLM:
    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.feeds: list[str] = []

    def invoke(self, prompt: Any, config: Any = None) -> Any:
        self.feeds.append(str(prompt))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _Storage(MemoryStorage):
    def __init__(self) -> None:
        self.memory: dict[str, Any] = {"version": "2.0", "revision": 0, "user": {}, "history": {}, "facts": []}

    def load(self, agent_name=None, *, user_id=None):
        return json.loads(json.dumps(self.memory))

    def reload(self, agent_name=None, *, user_id=None):
        return self.load(agent_name, user_id=user_id)

    def save(self, memory_data, agent_name=None, *, user_id=None, expected_revision=None):
        self.memory = json.loads(json.dumps(memory_data))
        return True


def _turns(*texts: str) -> list[Any]:
    out: list[Any] = []
    for text in texts:
        out += [HumanMessage(content=text), AIMessage(content=f"reply-{text}")]
    return out


def test_build_llm_passes_the_caps(monkeypatch):
    seen: dict[str, Any] = {}

    def fake_init_chat_model(**kwargs):
        seen.update(kwargs)
        return object()

    import langchain.chat_models

    monkeypatch.setattr(langchain.chat_models, "init_chat_model", fake_init_chat_model)
    llm_module.build_llm(DeerMemModelConfig(model="glm-5.3-flash", base_url="http://litellm:4000", max_tokens=98304, request_timeout_seconds=2400, max_retries=0))
    assert seen["max_tokens"] == 98304 and seen["timeout"] == 2400 and seen["max_retries"] == 0


def test_build_llm_default_passes_no_caps(monkeypatch):
    seen: dict[str, Any] = {}
    import langchain.chat_models

    monkeypatch.setattr(langchain.chat_models, "init_chat_model", lambda **kwargs: seen.update(kwargs) or object())
    llm_module.build_llm(DeerMemModelConfig(model="glm-5.3-flash"))
    assert not {"max_tokens", "timeout", "max_retries"} & set(seen)


def test_default_keeps_refeeding_a_failing_batch():
    llm = _ScriptedLLM(_Response("not json"), _Response("not json"), _Response("not json"))
    updater = MemoryUpdater(DeerMemConfig(), _Storage(), llm)
    messages = _turns("first")
    for _ in range(3):
        assert updater.update_memory(messages, thread_id="t", agent_name="a", user_id="u") is False
    assert len(llm.feeds) == 3


def test_batch_is_skipped_after_n_failures_then_new_turns_flow(caplog):
    caplog.set_level(logging.WARNING)
    llm = _ScriptedLLM(_Response("x" * 10, finish_reason="length"), TimeoutError("read timeout"), _Response(_EMPTY))
    updater = MemoryUpdater(DeerMemConfig(max_failed_batch_attempts=2), _Storage(), llm)
    first = _turns("huge")
    assert updater.update_memory(first, thread_id="t", agent_name="a", user_id="u") is False
    assert updater.update_memory(first, thread_id="t", agent_name="a", user_id="u") is False
    assert any("cut at max_tokens" in r.getMessage() for r in caplog.records)
    assert any("skipping that batch" in r.getMessage() for r in caplog.records)

    # the skipped batch is not re-fed; only the new turn is
    later = first + _turns("next")
    assert updater.update_memory(later, thread_id="t", agent_name="a", user_id="u") is True
    assert "huge" not in llm.feeds[-1] and "next" in llm.feeds[-1]


def test_a_success_resets_the_count():
    llm = _ScriptedLLM(_Response("bad"), _Response(_EMPTY), _Response("bad"), _Response(_EMPTY))
    updater = MemoryUpdater(DeerMemConfig(max_failed_batch_attempts=2), _Storage(), llm)
    msgs = _turns("a")
    assert updater.update_memory(msgs, thread_id="t", agent_name="x", user_id="u") is False
    assert updater.update_memory(msgs, thread_id="t", agent_name="x", user_id="u") is True
    msgs2 = msgs + _turns("b")
    # one failure after a success is not yet a skip: the batch is fed again
    assert updater.update_memory(msgs2, thread_id="t", agent_name="x", user_id="u") is False
    assert updater.update_memory(msgs2, thread_id="t", agent_name="x", user_id="u") is True
    assert "reply-b" in llm.feeds[-1]

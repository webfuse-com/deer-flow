"""[argus patch #123] Title generation that does not hold up the run.

A title call is a second model call inside the first model step. On
atlas-nicholas (2026-10-12) it added 1.6-4.7 s to every new thread and once
hung for 71 s on a stalled stream. ``title.timeout_seconds`` caps it and
``title.background`` lets the run continue while it runs.
"""

import asyncio
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage

from deerflow.agents.middlewares.title_middleware import TitleMiddleware
from deerflow.config.title_config import TitleConfig

STATE = {"messages": [HumanMessage(content="Plan the Ariadne rollout"), AIMessage(content="Sure")]}


def _runtime(thread_id="t1"):
    return SimpleNamespace(context={"thread_id": thread_id})


def _middleware(monkeypatch, *, delay=0.0, title="Ariadne rollout plan", **config):
    cfg = TitleConfig(model_name="title-model", **config)
    middleware = TitleMiddleware(title_config=cfg, extensions=())
    calls = []

    async def fake_generate(state, *, task_store=None):
        calls.append(state)
        await asyncio.sleep(delay)
        return {"title": title}

    monkeypatch.setattr(middleware, "_agenerate_title_result", fake_generate)
    return middleware, calls


def test_default_waits_for_the_title_as_upstream(monkeypatch):
    middleware, _ = _middleware(monkeypatch)
    assert asyncio.run(middleware.aafter_model(STATE, runtime=_runtime())) == {"title": "Ariadne rollout plan"}


def test_timeout_falls_back_to_the_local_title(monkeypatch):
    middleware, _ = _middleware(monkeypatch, delay=5, timeout_seconds=0.05)
    result = asyncio.run(middleware.aafter_model(STATE, runtime=_runtime()))
    assert result == {"title": "Plan the Ariadne rollout"}


def test_background_title_lands_at_a_later_model_step(monkeypatch):
    middleware, calls = _middleware(monkeypatch, delay=0.01, background=True, timeout_seconds=5)

    async def run():
        first = await middleware.aafter_model(STATE, runtime=_runtime())
        await asyncio.sleep(0.05)
        second = await middleware.aafter_model(STATE, runtime=_runtime())
        third = await middleware.aafter_model({**STATE, "title": "Ariadne rollout plan"}, runtime=_runtime())
        return first, second, third

    first, second, third = asyncio.run(run())
    assert first is None
    assert second == {"title": "Ariadne rollout plan"}
    assert third is None and len(calls) == 1


def test_background_title_is_collected_at_the_end_of_the_run(monkeypatch):
    middleware, _ = _middleware(monkeypatch, delay=0.05, background=True, timeout_seconds=5)

    async def run():
        assert await middleware.aafter_model(STATE, runtime=_runtime()) is None
        return await middleware.aafter_agent(STATE, runtime=_runtime())

    assert asyncio.run(run()) == {"title": "Ariadne rollout plan"}


def test_background_wait_at_the_end_is_bounded(monkeypatch):
    middleware, _ = _middleware(monkeypatch, delay=5, background=True, timeout_seconds=0.05)

    async def run():
        await middleware.aafter_model(STATE, runtime=_runtime())
        return await middleware.aafter_agent(STATE, runtime=_runtime())

    assert asyncio.run(run()) == {"title": "Plan the Ariadne rollout"}


def test_background_threads_do_not_share_a_pending_title(monkeypatch):
    middleware, calls = _middleware(monkeypatch, delay=0.01, background=True, timeout_seconds=5)

    async def run():
        await middleware.aafter_model(STATE, runtime=_runtime("a"))
        await middleware.aafter_model(STATE, runtime=_runtime("b"))
        return await middleware.aafter_agent(STATE, runtime=_runtime("a")), await middleware.aafter_agent(STATE, runtime=_runtime("b"))

    assert asyncio.run(run()) == ({"title": "Ariadne rollout plan"}, {"title": "Ariadne rollout plan"})
    assert len(calls) == 2


def test_background_without_a_title_model_answers_locally_at_once(monkeypatch):
    middleware = TitleMiddleware(title_config=TitleConfig(model_name=None, background=True), extensions=())
    assert asyncio.run(middleware.aafter_model(STATE, runtime=_runtime())) == {"title": "Plan the Ariadne rollout"}


def test_run_end_without_pending_title_is_a_no_op(monkeypatch):
    middleware, _ = _middleware(monkeypatch, background=True)
    assert asyncio.run(middleware.aafter_agent(STATE, runtime=_runtime())) is None


def test_markdown_wrapping_is_stripped_from_the_title():
    middleware = TitleMiddleware(title_config=TitleConfig(model_name="title-model"), extensions=())
    assert middleware._parse_title("**Ariadne Rollout Planning Blueprint**") == "Ariadne Rollout Planning Blueprint"
    assert middleware._parse_title("# Plan the rollout") == "Plan the rollout"
    assert middleware._parse_title('"Quoted title"') == "Quoted title"

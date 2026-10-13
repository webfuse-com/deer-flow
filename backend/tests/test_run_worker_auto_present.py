"""[argus patch #133] Outputs a run produced but did not present are presented, not a failed run."""

from types import SimpleNamespace
from typing import Annotated, TypedDict
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from deerflow.agents.thread_state import merge_artifacts
from deerflow.config.app_config import AppConfig
from deerflow.config.artifact_delivery_config import ArtifactDeliveryConfig
from deerflow.config.sandbox_config import SandboxConfig
from deerflow.runtime.events.store.memory import MemoryRunEventStore
from deerflow.runtime.runs.manager import RunManager
from deerflow.runtime.runs.schemas import RunStatus
from deerflow.runtime.runs.worker import RunContext, _auto_present_outputs, run_agent

PRODUCED = ["/mnt/user-data/outputs/runbook-v5.md"]
UNPRESENTED = {"presented": 0, "paths": [], "by_tool": {}, "produced_paths": PRODUCED, "matched_paths": [], "stage": "not_started", "satisfied": False}


def _make_bridge():
    return SimpleNamespace(publish=AsyncMock(), publish_end=AsyncMock(), cleanup=AsyncMock())


class _State(TypedDict, total=False):
    messages: list
    artifacts: Annotated[list[str], merge_artifacts]


def _graph():
    builder = StateGraph(_State)
    builder.add_node("model", lambda state: {"messages": [AIMessage(content="done")]})
    builder.add_node("after", lambda state: {})
    builder.add_edge(START, "model")
    builder.add_edge("model", "after")
    builder.add_edge("after", END)
    return builder.compile(checkpointer=InMemorySaver())


@pytest.mark.anyio
async def test_auto_present_writes_artifacts_and_leaves_no_pending_step():
    graph = _graph()
    config = {"configurable": {"thread_id": "t-auto"}}
    await graph.ainvoke({"messages": [], "artifacts": ["/mnt/user-data/outputs/old.md"]}, config)
    app_config = SimpleNamespace(artifact_delivery=ArtifactDeliveryConfig(auto_present=True))

    result = await _auto_present_outputs(graph, config, UNPRESENTED, app_config)

    assert result is not None and result["satisfied"] is True and result["stage"] == "auto_presented"
    assert result["auto_presented_paths"] == PRODUCED
    state = await graph.aget_state(config)
    assert state.values["artifacts"] == ["/mnt/user-data/outputs/old.md", *PRODUCED]
    assert state.next == ()


@pytest.mark.anyio
async def test_off_by_default():
    graph = _graph()
    assert await _auto_present_outputs(graph, {"configurable": {"thread_id": "t"}}, UNPRESENTED, SimpleNamespace(artifact_delivery=ArtifactDeliveryConfig())) is None


@pytest.mark.anyio
async def test_too_many_outputs_are_not_presented():
    content = {**UNPRESENTED, "produced_paths": [f"/mnt/user-data/outputs/f{i}.txt" for i in range(5)]}
    app_config = SimpleNamespace(artifact_delivery=ArtifactDeliveryConfig(auto_present=True, max_paths=3))
    assert await _auto_present_outputs(_graph(), {"configurable": {"thread_id": "t"}}, content, app_config) is None


@pytest.mark.anyio
async def test_a_failed_state_update_keeps_the_error():
    agent = SimpleNamespace(aupdate_state=AsyncMock(side_effect=ValueError("no checkpointer")))
    app_config = SimpleNamespace(artifact_delivery=ArtifactDeliveryConfig(auto_present=True))
    assert await _auto_present_outputs(agent, {}, UNPRESENTED, app_config) is None


@pytest.mark.anyio
async def test_run_succeeds_with_auto_presented_delivery(monkeypatch):
    run_manager = RunManager()
    record = await run_manager.create("thread-1")
    store = MemoryRunEventStore()
    monkeypatch.setattr("deerflow.runtime.runs.worker._produced_output_paths", AsyncMock(return_value=PRODUCED))
    updates = []

    class ProseOnlyAgent:
        async def astream(self, graph_input, config=None, stream_mode=None, subgraphs=False):
            yield {"messages": [AIMessage(content="Here is version 5.")]}

        async def aupdate_state(self, config, values):
            updates.append(values)

    app_config = AppConfig(sandbox=SandboxConfig(use="test"))
    app_config.artifact_delivery.auto_present = True
    await run_agent(
        _make_bridge(),
        run_manager,
        record,
        ctx=RunContext(checkpointer=None, event_store=store, app_config=app_config),
        agent_factory=lambda *, config: ProseOnlyAgent(),
        graph_input={},
        config={},
    )

    assert updates == [{"artifacts": PRODUCED}]
    assert record.status == RunStatus.success
    events = [e for e in await store.list_events("thread-1", record.run_id) if e["event_type"] == "run.delivery"]
    assert events[0]["content"]["stage"] == "auto_presented"
    assert events[0]["content"]["satisfied"] is True

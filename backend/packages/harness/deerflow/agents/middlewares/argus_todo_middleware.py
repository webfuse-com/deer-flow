"""Argus variant of TodoMiddleware that defers planning judgment to the planner skill.

Upstream's TodoMiddleware injects a system prompt and tool description telling
the agent NOT to use ``write_todos`` for "simple tasks (< 3 steps)" and to make
its own judgment about when planning is warranted. Argus's qwen-local-coder
agent has a planner SKILL.md that decides when to plan and what the steps are;
``write_todos`` is then used to mirror those steps into ``state.todos[]`` for
live UI tick-off.

The two systems contradict each other if both are active: the agent reads
upstream's "skip this for simple tasks" guidance and skips the planner on
bare prompts the planner SKILL.md would have caught. This subclass overrides
only the prompt and tool-description text so the agent reads "the planner
decides" instead. All other behavior — context-loss reminders in
``before_model``, premature-exit prevention in ``after_model``, async hooks —
inherits from the parent unchanged.

Selected by ``deerflow.agents.lead_agent.agent._create_todo_list_middleware``
for agents whose config sets ``uses_planner_pipeline: true`` (e.g.
glm-planner; historically keyed on ``agent_name == "qwen-local-coder"``).
Other agents continue to receive the upstream prompt.

[argus patch #41] also normalizes a double-encoded ``write_todos`` arg (the
model passing ``todos`` as a JSON string instead of a list) in ``after_model``
— see ``_coerce_stringified_todos``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import AIMessage

from .todo_middleware import TodoMiddleware

logger = logging.getLogger(__name__)


def _coerce_stringified_todos(message: AIMessage) -> int:
    """[argus patch #41] Normalize a JSON-string ``todos`` arg to a list.

    glm-nw (and peers) sometimes double-encode the ``write_todos`` argument:
    the tool call arrives with ``args.todos`` as the STRING
    ``'[{"content": ..., "status": ...}, ...]'`` instead of a native array.
    Pydantic then rejects the call (``todos: list[Todo]``), the agent gets an
    error ToolMessage, and ``state.todos[]`` never hydrates — observed on the
    weekly eval 2026-07-02 (pythia/planning, glm-planner). The payload is
    valid; only the encoding is wrong, so parse it in place before tool
    validation runs. Anything that does not parse to a list is left alone
    for the normal validation error path. Returns the number of calls fixed.
    """
    fixed = 0
    for tc in message.tool_calls or []:
        if tc.get("name") != "write_todos":
            continue
        args = tc.get("args") or {}
        todos = args.get("todos")
        if not isinstance(todos, str):
            continue
        try:
            parsed = json.loads(todos)
        except ValueError:
            continue
        if isinstance(parsed, list):
            args["todos"] = parsed
            fixed += 1
    return fixed


_ARGUS_SYSTEM_PROMPT = """
<todo_list_system>
You have access to the `write_todos` tool. It maintains a live progress view in the user's UI by writing the current step list into `state.todos[]`.

The `planner` skill (read /mnt/skills/public/planner/SKILL.md) is the source of truth for when to plan and what the steps are. If the planner writes a real plan to /mnt/user-data/workspace/plan.json, mirror its `steps[]` into write_todos:

- One todo per step, content = step.action.
- First todo `status: "in_progress"` (about to start).
- Subsequent todos `status: "pending"`.
- Before starting step Si (i > 1): flip Si-1 to "completed" and Si to "in_progress" via another write_todos call.
- After the last step: one final write_todos call marking it "completed".

If the planner returns the skip form (`{"status": "skip", ...}`), do NOT call write_todos at all. The user gets a direct answer.

The planner's decision supersedes any general guidance about "complex vs. simple tasks." If the planner produced a real plan, mirror it. If it skipped, don't.
</todo_list_system>
"""


_ARGUS_TOOL_DESCRIPTION = """Mirror the planner's plan.json steps[] into the live state.todos[] for UI tick-off.

Call this AFTER the planner skill has written a real plan to `/mnt/user-data/workspace/plan.json`. Do NOT call it if the planner returned `{"status":"skip",...}` — that's a trivial request and there's nothing to track.

## Args

- `todos`: a list of `{content: str, status: str}` items, one per plan.json step. The first item's status is `"in_progress"` (you're about to execute it); subsequent items are `"pending"`.

## Lifecycle

1. **Initial hydration** (after plan.json write): one todo per step, first `in_progress`, rest `pending`.
2. **Mid-execution** (between steps): the previous step flips to `"completed"`, the current step flips to `"in_progress"`. Send the full updated array each time — the tool replaces state, doesn't merge.
3. **End** (after the last step): the last item flips to `"completed"`.

## Statuses

- `pending`: not yet started.
- `in_progress`: actively being worked on. Keep exactly one at a time unless the plan marked steps as parallel.
- `completed`: finished. Only mark this when the step's `produces[]` are actually present.
"""


class ArgusTodoMiddleware(TodoMiddleware):
    """``TodoMiddleware`` variant whose default prompts defer to the planner skill.

    Behavior (context-loss reminders, premature-exit prevention) inherits
    unchanged from the parent class. Only the user-facing system prompt and
    tool description differ.

    Callers can still override either prompt explicitly via the constructor
    kwargs; the Argus defaults apply only when no value is supplied.
    """

    def __init__(
        self,
        system_prompt: str | None = None,
        tool_description: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(
            system_prompt=system_prompt if system_prompt is not None else _ARGUS_SYSTEM_PROMPT,
            tool_description=tool_description if tool_description is not None else _ARGUS_TOOL_DESCRIPTION,
            **kwargs,
        )

    def after_model(
        self,
        state: dict[str, Any],
        runtime: Any,
    ) -> dict[str, Any] | None:
        """[argus patch #41] Coerce stringified todos, then run parent logic.

        after_model runs between the model response and tool execution, so
        rewriting the tool-call args in place here means (a) pydantic sees a
        valid list, (b) the trajectory records the normalized call — eval
        graders stay strict — and (c) the parent's premature-exit and
        parallel-call checks operate on the corrected message.
        """
        messages = state.get("messages") or []
        if messages and isinstance(messages[-1], AIMessage):
            fixed = _coerce_stringified_todos(messages[-1])
            if fixed:
                logger.info(
                    "[argus patch #41] coerced stringified todos arg in %d write_todos call(s)",
                    fixed,
                )
        return super().after_model(state, runtime)

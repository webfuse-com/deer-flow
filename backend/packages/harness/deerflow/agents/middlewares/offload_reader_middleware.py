"""[argus patch #144] Offloaded tool results stay readable in allowlisted runs.

The tool-output budget writes a large result to
``/mnt/user-data/outputs/<storage_subdir>/`` and leaves a preview whose only
pointer to the data is "use read_file on <path>". A run whose tool allowlist
(the agent's ``allowed_tools`` or a schedule's ``allowed-tools``, under
``tool_policy.source: agent``) leaves ``read_file`` out cannot follow it:
every atlas-quinten outreach and ICP run got a 167 KB ``get_leads`` result it
could not read, and every atlas-yauhen Kairos sync answered from a preview
(49 runs with an invalid-tool call in the week to 2026-10-19).

With ``tool_output.read_offloads_in_allowlisted_runs`` the build binds the
regular ``read_file`` tool for such a run and installs
``OffloadReaderScopeMiddleware``, which refuses any ``read_file`` path that is
not an offload of this run before the tool runs: the path must be one a tool
result of the current run (after its last user message) was offloaded to.
That data was returned to this run by a tool it was allowed to call, so the
grant adds no new authority. A schedule on a Telegram-bound or ``thread:``
chat cannot read what earlier interactive runs offloaded with tools the
schedule was not granted (Gmail, BigQuery).

The read happens on the host, where the budget middleware wrote the file:
``read_file`` itself goes through ``ensure_sandbox_initialized``, so an
app-only schedule (no sandbox tools, no #132 prewarm) would start a container
for its first offload read. Without a host copy (a remote sandbox keeps the
offload inside the sandbox) the regular tool reads it.
"""

from __future__ import annotations

import logging
import os
import posixpath
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, override

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from deerflow.agents.middlewares.message_utils import is_genuine_user_message
from deerflow.agents.middlewares.tool_transform_meta import read_tool_transforms
from deerflow.config.paths import VIRTUAL_PATH_PREFIX

logger = logging.getLogger(__name__)

READ_FILE_TOOL_NAME = "read_file"
_BUDGET_MIDDLEWARE = "ToolOutputBudgetMiddleware"


def offload_directory(storage_subdir: str) -> str:
    """The virtual directory the tool-output budget offloads results into."""
    return f"{VIRTUAL_PATH_PREFIX}/outputs/{storage_subdir}"


def offload_reader_grant(candidate_tools: Sequence[Any], allowed_tools: list[Any], app_config: Any) -> tuple[list[Any], AgentMiddleware | None]:
    """Add a scoped ``read_file`` when the allowlist removed it.

    ``candidate_tools`` is the tool list before the allowlist filter,
    ``allowed_tools`` the list after it. Returns the tool list to continue
    with and the scope middleware to install, or the list unchanged and None.
    """
    output_config = getattr(app_config, "tool_output", None)
    if output_config is None or getattr(output_config, "read_offloads_in_allowlisted_runs", False) is not True:
        return allowed_tools, None
    if not getattr(output_config, "enabled", False):
        return allowed_tools, None
    if any(getattr(tool, "name", None) == READ_FILE_TOOL_NAME for tool in allowed_tools):
        return allowed_tools, None
    reader = next((tool for tool in candidate_tools if getattr(tool, "name", None) == READ_FILE_TOOL_NAME), None)
    if reader is None:
        return allowed_tools, None
    return [*allowed_tools, reader], OffloadReaderScopeMiddleware(offload_directory(output_config.storage_subdir), storage_subdir=output_config.storage_subdir)


def _messages(request: ToolCallRequest) -> list[Any]:
    state = getattr(request, "state", None)
    if not isinstance(state, Mapping):
        state = getattr(getattr(request, "runtime", None), "state", None)
    messages = state.get("messages") if isinstance(state, Mapping) else None
    return messages if isinstance(messages, list) else []


def _outputs_path(request: ToolCallRequest) -> str | None:
    for state in (getattr(request, "state", None), getattr(getattr(request, "runtime", None), "state", None)):
        thread_data = state.get("thread_data") if isinstance(state, Mapping) else None
        outputs_path = thread_data.get("outputs_path") if isinstance(thread_data, Mapping) else None
        if isinstance(outputs_path, str) and outputs_path:
            return outputs_path
    return None


def _message_text(message: ToolMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(block if isinstance(block, str) else str(block.get("text", "")) for block in content if isinstance(block, (str, dict)))
    return ""


def _offloaded_this_run(messages: list[Any], path: str) -> bool:
    """True when a tool result of the current run was offloaded to *path*."""
    # The path ends where a filename character cannot follow (a sentence's
    # closing period can: "saved to <path>.").
    named = re.compile(re.escape(path) + r"(?![\w-]|\.\w)")
    for message in reversed(messages):
        if is_genuine_user_message(message):
            return False
        if not isinstance(message, ToolMessage):
            continue
        if not any(entry.get("kind") == "externalized" and entry.get("by") == _BUDGET_MIDDLEWARE for entry in read_tool_transforms(message)):
            continue
        from deerflow.agents.middlewares.tool_output_budget_middleware import TOOL_OUTPUT_BLOB_KEY

        blob = (message.additional_kwargs or {}).get(TOOL_OUTPUT_BLOB_KEY)
        if isinstance(blob, Mapping) and blob.get("virtual_path") == path:
            return True
        if named.search(_message_text(message)):
            return True
    return False


class OffloadReaderScopeMiddleware(AgentMiddleware):
    """Scope ``read_file`` to this run's offloads in a run granted it by #144, and read them on the host."""

    def __init__(self, directory: str, *, storage_subdir: str | None = None) -> None:
        super().__init__()
        self._directory = posixpath.normpath(directory)
        self._storage_subdir = storage_subdir or posixpath.basename(self._directory)

    def allows(self, path: object) -> bool:
        if not isinstance(path, str) or not path.strip():
            return False
        normalized = posixpath.normpath(path.strip())
        name = posixpath.basename(normalized)
        return posixpath.dirname(normalized) == self._directory and name not in ("", ".", "..")

    def _refuse(self, request: ToolCallRequest, reason: str) -> ToolMessage:
        return ToolMessage(
            content=reason,
            tool_call_id=str(request.tool_call.get("id") or ""),
            name=READ_FILE_TOOL_NAME,
            status="error",
        )

    def _answer(self, request: ToolCallRequest) -> ToolMessage | None:
        """A refusal or a host-side read, or None to let the tool run."""
        if request.tool_call.get("name") != READ_FILE_TOOL_NAME:
            return None
        args = request.tool_call.get("args")
        args = args if isinstance(args, dict) else {}
        path = args.get("path")
        if not self.allows(path):
            return self._refuse(request, f"Error: read_file in this run can only read offloaded tool results in {self._directory}/ (the path a tool result preview names). Other files are outside this run's allowed tools.")
        path = posixpath.normpath(path.strip())
        if not _offloaded_this_run(_messages(request), path):
            return self._refuse(request, f"Error: read_file in this run can only read tool results offloaded during this run (the path a preview in this run names). {path} was not offloaded in this run.")
        content = self._read_on_host(request, path, args)
        if content is None:
            return None
        return ToolMessage(content=content, tool_call_id=str(request.tool_call.get("id") or ""), name=READ_FILE_TOOL_NAME)

    def _read_on_host(self, request: ToolCallRequest, path: str, args: dict) -> str | None:
        """read_file's answer from the host copy, or None when there is none (or the arguments need the tool)."""
        from deerflow.agents.middlewares.tool_output_budget_middleware import _host_path_for_externalized_output

        outputs_path = _outputs_path(request)
        if outputs_path is None:
            return None
        host_path = _host_path_for_externalized_output(path, outputs_path=outputs_path, storage_subdir=self._storage_subdir)
        if host_path is None or not os.path.isfile(host_path):
            return None
        start_line, end_line = args.get("start_line"), args.get("end_line")
        if any(value is not None and (type(value) is not int) for value in (start_line, end_line)):
            return None  # the tool's own argument validation answers these
        try:
            return _format_read(host_path, path, start_line, end_line)
        except (OSError, UnicodeDecodeError):
            logger.warning("Offload host read failed for %s; falling back to read_file", path, exc_info=True)
            return None

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        answer = self._answer(request)
        return answer if answer is not None else handler(request)

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        answer = self._answer(request)
        return answer if answer is not None else await handler(request)


def _format_read(host_path: str, path: str, start_line: int | None, end_line: int | None) -> str:
    """The text read_file returns for this file and range (local-sandbox line semantics)."""
    from deerflow.sandbox import tools as sandbox_tools

    if start_line is not None and start_line < 1:
        return sandbox_tools.READ_FILE_INVALID_START_LINE
    effective_start = start_line or 1
    if end_line is not None and end_line < 1:
        return sandbox_tools.READ_FILE_INVALID_END_LINE
    if end_line is not None and effective_start > end_line:
        return sandbox_tools.READ_FILE_INVALID_RANGE
    use_line_range = start_line is not None or end_line is not None
    line_count = 0
    with open(host_path, encoding="utf-8", newline="\n") as handle:
        if not use_line_range:
            content = handle.read()
        else:
            selected: list[str] = []
            for line_number, line in enumerate(handle, start=1):
                line_count = line_number
                if line_number < effective_start:
                    continue
                if end_line is not None and line_number > end_line:
                    break
                selected.append(line.rstrip("\r\n"))
            content = "\n".join(selected)
    if not content:
        if start_line is not None and start_line > 1 and start_line > line_count:
            return sandbox_tools.READ_FILE_START_LINE_EXCEEDS
        return sandbox_tools.READ_FILE_EMPTY
    try:
        from deerflow.config.app_config import get_app_config

        sandbox_config = get_app_config().sandbox
        max_chars = sandbox_config.read_file_output_max_chars if sandbox_config else 50000
    except Exception:
        max_chars = 50000
    return sandbox_tools._truncate_read_file_output(content, max_chars, line_offset=effective_start - 1, joined_lines=use_line_range, ends_at_eof=end_line is None)

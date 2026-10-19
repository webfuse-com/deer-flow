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
``OffloadReaderScopeMiddleware``, which refuses any ``read_file`` path outside
the thread's offload directory before the tool runs. The data was already
returned to this thread by a tool the run was allowed to call, so the grant
adds no new authority.
"""

from __future__ import annotations

import posixpath
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, override

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from deerflow.config.paths import VIRTUAL_PATH_PREFIX

READ_FILE_TOOL_NAME = "read_file"


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
    return [*allowed_tools, reader], OffloadReaderScopeMiddleware(offload_directory(output_config.storage_subdir))


class OffloadReaderScopeMiddleware(AgentMiddleware):
    """Refuse ``read_file`` outside the offload directory in a run granted it by #144."""

    def __init__(self, directory: str) -> None:
        super().__init__()
        self._directory = posixpath.normpath(directory)

    def allows(self, path: object) -> bool:
        if not isinstance(path, str) or not path.strip():
            return False
        normalized = posixpath.normpath(path.strip())
        name = posixpath.basename(normalized)
        return posixpath.dirname(normalized) == self._directory and name not in ("", ".", "..")

    def _refusal(self, request: ToolCallRequest) -> ToolMessage | None:
        if request.tool_call.get("name") != READ_FILE_TOOL_NAME:
            return None
        args = request.tool_call.get("args")
        if self.allows(args.get("path") if isinstance(args, dict) else None):
            return None
        return ToolMessage(
            content=(f"Error: read_file in this run can only read offloaded tool results in {self._directory}/ (the path a tool result preview names). Other files are outside this run's allowed tools."),
            tool_call_id=str(request.tool_call.get("id") or ""),
            name=READ_FILE_TOOL_NAME,
            status="error",
        )

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        refusal = self._refusal(request)
        return refusal if refusal is not None else handler(request)

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        refusal = self._refusal(request)
        return refusal if refusal is not None else await handler(request)

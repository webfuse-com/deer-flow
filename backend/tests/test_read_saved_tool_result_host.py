"""[argus patch #166] read_file serves saved tool results from the host.

ToolOutputBudgetMiddleware writes an oversized tool result into the thread's
host outputs directory and tells the model to read it back. With a mounting
provider that file is the same one the sandbox shows at
``/mnt/user-data/outputs/.tool-results/``, so reading it must not start a
sandbox container for a thread that has none yet. Anything unusual (a
non-mounting provider, a missing file, a link, a FIFO) falls back to the
sandbox read unchanged.
"""

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from deerflow.sandbox import tools
from deerflow.sandbox.exceptions import SandboxAuthorizationError
from deerflow.sandbox.local.local_sandbox import LocalSandbox
from deerflow.sandbox.tools import read_file_tool

_SAVED = "/mnt/user-data/outputs/.tool-results/calendar_list_events-abc.txt"
_CONTENT = '{\n  "events": [\n    {"summary": "Kickoff"},\r\n    {"summary": "Week Prep"}\n  ]\n}\n'


class _Provider:
    def __init__(self, mounts: bool = True) -> None:
        self.uses_thread_data_mounts = mounts


class _FakeSandbox:
    def __init__(self) -> None:
        self.reads: list[str] = []

    def read_file(self, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        self.reads.append(path)
        return "from-sandbox"


@pytest.fixture
def thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    outputs = tmp_path / "outputs"
    (outputs / ".tool-results").mkdir(parents=True)
    (outputs / ".tool-results" / "calendar_list_events-abc.txt").write_text(_CONTENT, encoding="utf-8", newline="")
    runtime = SimpleNamespace(
        state={"thread_data": {"outputs_path": str(outputs), "workspace_path": str(tmp_path / "ws"), "uploads_path": str(tmp_path / "up")}},
        context={"thread_id": "t1"},
        config={},
    )
    sandbox = _FakeSandbox()
    calls = {"sync": 0, "async": 0}

    def _acquire(runtime):
        calls["sync"] += 1
        return sandbox

    async def _acquire_async(runtime):
        calls["async"] += 1
        return sandbox

    monkeypatch.setattr(tools, "get_initialized_sandbox_provider", lambda: _Provider())
    monkeypatch.setattr(tools, "ensure_sandbox_initialized", _acquire)
    monkeypatch.setattr(tools, "ensure_sandbox_initialized_async", _acquire_async)
    monkeypatch.setattr(tools, "ensure_thread_directories_exist", lambda runtime: None)
    monkeypatch.setattr(tools, "safe_app_config", lambda: None)
    return SimpleNamespace(runtime=runtime, outputs=outputs, sandbox=sandbox, calls=calls)


def _read(thread, path: str = _SAVED, **kwargs) -> str:
    return read_file_tool.func(runtime=thread.runtime, description="", path=path, **kwargs)


def _read_async(thread, path: str = _SAVED, **kwargs) -> str:
    return asyncio.run(tools._read_file_tool_async(thread.runtime, path, "", kwargs.get("start_line"), kwargs.get("end_line")))


def test_saved_result_is_read_on_host_without_a_sandbox(thread) -> None:
    assert _read(thread) == _CONTENT
    assert thread.calls == {"sync": 0, "async": 0}


def test_async_entry_skips_the_lazy_sandbox_acquire(thread) -> None:
    assert _read_async(thread) == _CONTENT
    assert thread.calls == {"sync": 0, "async": 0}


def test_ranged_reads_match_local_sandbox(thread) -> None:
    host_file = str(thread.outputs / ".tool-results" / "calendar_list_events-abc.txt")
    local = LocalSandbox("t1")
    for start, end in [(1, 2), (2, 3), (3, None), (None, 2), (5, 9), (2, 2)]:
        assert tools._read_saved_tool_result(thread.runtime, _SAVED, start_line=start, end_line=end) == local.read_file(host_file, start_line=start, end_line=end)
    assert tools._read_saved_tool_result(thread.runtime, _SAVED) == local.read_file(host_file)
    assert _read(thread, start_line=2, end_line=3) == '  "events": [\n    {"summary": "Kickoff"},'


def test_start_beyond_end_keeps_the_tool_contract(thread) -> None:
    assert _read(thread, start_line=50) == tools.READ_FILE_START_LINE_EXCEEDS
    assert thread.calls["sync"] == 0


@pytest.mark.parametrize(
    "path",
    [
        "/mnt/user-data/outputs/report.txt",
        "/mnt/user-data/outputs/.tool-results/../report.txt",
        "/mnt/user-data/outputs/.tool-results/sub/x.txt",
        "/mnt/user-data/outputs/.tool-results/",
        "/mnt/user-data/outputs//.tool-results/x.txt",
        "/mnt/user-data/workspace/.tool-results/x.txt",
    ],
)
def test_other_paths_are_not_saved_results(path) -> None:
    assert tools._saved_tool_result_name(path) is None


def test_configured_storage_subdir_is_honored(thread, monkeypatch) -> None:
    (thread.outputs / "spill").mkdir()
    (thread.outputs / "spill" / "x.txt").write_text("custom", encoding="utf-8")
    monkeypatch.setattr(tools, "safe_app_config", lambda: SimpleNamespace(tool_output=SimpleNamespace(storage_subdir="spill")))
    assert _read(thread, path="/mnt/user-data/outputs/spill/x.txt") == "custom"
    assert tools._saved_tool_result_name(_SAVED) is None


def test_non_mounting_provider_reads_through_the_sandbox(thread, monkeypatch) -> None:
    monkeypatch.setattr(tools, "get_initialized_sandbox_provider", lambda: _Provider(mounts=False))
    assert _read_async(thread) == "from-sandbox"
    assert thread.calls["async"] == 1


def test_missing_file_reads_through_the_sandbox(thread) -> None:
    assert _read(thread, path="/mnt/user-data/outputs/.tool-results/gone.txt") == "from-sandbox"
    assert thread.sandbox.reads == ["/mnt/user-data/outputs/.tool-results/gone.txt"]


def test_symlinked_file_is_not_followed(thread, tmp_path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("gateway-only", encoding="utf-8")
    os.symlink(secret, thread.outputs / ".tool-results" / "link.txt")
    assert _read(thread, path="/mnt/user-data/outputs/.tool-results/link.txt") == "from-sandbox"


def test_symlinked_results_directory_is_not_followed(thread, tmp_path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "x.txt").write_text("gateway-only", encoding="utf-8")
    results = thread.outputs / ".tool-results"
    for child in results.iterdir():
        child.unlink()
    results.rmdir()
    os.symlink(elsewhere, results)
    assert _read(thread, path="/mnt/user-data/outputs/.tool-results/x.txt") == "from-sandbox"


def test_fifo_does_not_block_and_falls_back(thread) -> None:
    os.mkfifo(thread.outputs / ".tool-results" / "pipe.txt")
    assert _read(thread, path="/mnt/user-data/outputs/.tool-results/pipe.txt") == "from-sandbox"


def test_sandbox_authorization_still_applies(thread, monkeypatch) -> None:
    def _deny(**kwargs):
        raise SandboxAuthorizationError()

    monkeypatch.setattr(tools, "authorize_sandbox_execution", _deny)
    monkeypatch.setattr(tools, "authorize_sandbox_execution_async", _async_deny)
    assert _read(thread).startswith("Error:")
    assert _read_async(thread).startswith("Error:")
    assert _CONTENT not in _read(thread)


async def _async_deny(**kwargs):
    raise SandboxAuthorizationError()

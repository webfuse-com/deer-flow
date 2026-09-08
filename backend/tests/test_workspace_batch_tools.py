"""Bounded multi-file inspect and optimistic patch tools."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from deerflow.sandbox.local.local_sandbox import LocalSandbox
from deerflow.sandbox.tools import workspace_inspect_tool, workspace_patch_tool


def _sha(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _runtime(tmp_path: Path) -> SimpleNamespace:
    for subdir in ("workspace", "uploads", "outputs"):
        (tmp_path / subdir).mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        state={
            "sandbox": {"sandbox_id": "local:workspace-batch"},
            "thread_data": {
                "workspace_path": str(tmp_path / "workspace"),
                "uploads_path": str(tmp_path / "uploads"),
                "outputs_path": str(tmp_path / "outputs"),
            },
        },
        context={"thread_id": "workspace-batch"},
    )


def _wire(monkeypatch) -> None:
    monkeypatch.setattr("deerflow.sandbox.tools.ensure_sandbox_initialized", lambda runtime: LocalSandbox("workspace-batch"))
    monkeypatch.setattr("deerflow.sandbox.tools.ensure_thread_directories_exist", lambda runtime: None)


def test_workspace_inspect_returns_content_and_versions(tmp_path, monkeypatch) -> None:
    _wire(monkeypatch)
    runtime = _runtime(tmp_path)
    (tmp_path / "workspace" / "a.txt").write_text("alpha\n", encoding="utf-8")
    (tmp_path / "workspace" / "b.txt").write_text("beta\n", encoding="utf-8")

    result = json.loads(
        workspace_inspect_tool.func(
            runtime=runtime,
            description="inspect related files",
            files=[{"path": "/mnt/user-data/workspace/a.txt"}, {"path": "/mnt/user-data/workspace/b.txt"}],
        )
    )

    assert [item["content"] for item in result["files"]] == ["alpha\n", "beta\n"]
    assert result["files"][0]["sha256"] == _sha("alpha\n")


def test_workspace_patch_changes_multiple_files_atomically(tmp_path, monkeypatch) -> None:
    _wire(monkeypatch)
    runtime = _runtime(tmp_path)
    a = tmp_path / "workspace" / "a.txt"
    b = tmp_path / "workspace" / "b.txt"
    a.write_text("alpha\n", encoding="utf-8")
    b.write_text("beta\n", encoding="utf-8")

    result = json.loads(
        workspace_patch_tool.func(
            runtime=runtime,
            description="apply one edit plan",
            edits=[
                {
                    "path": "/mnt/user-data/workspace/a.txt",
                    "expected_sha256": _sha("alpha\n"),
                    "replacements": [{"old_str": "alpha", "new_str": "ALPHA"}],
                },
                {
                    "path": "/mnt/user-data/workspace/b.txt",
                    "expected_sha256": _sha("beta\n"),
                    "replacements": [{"old_str": "beta", "new_str": "BETA"}],
                },
            ],
        )
    )

    assert result["status"] == "success"
    assert a.read_text(encoding="utf-8") == "ALPHA\n"
    assert b.read_text(encoding="utf-8") == "BETA\n"


def test_workspace_patch_rejects_stale_plan_before_any_write(tmp_path, monkeypatch) -> None:
    _wire(monkeypatch)
    runtime = _runtime(tmp_path)
    a = tmp_path / "workspace" / "a.txt"
    b = tmp_path / "workspace" / "b.txt"
    a.write_text("changed\n", encoding="utf-8")
    b.write_text("beta\n", encoding="utf-8")

    result = workspace_patch_tool.func(
        runtime=runtime,
        description="reject stale edit plan",
        edits=[
            {
                "path": "/mnt/user-data/workspace/a.txt",
                "expected_sha256": _sha("alpha\n"),
                "replacements": [{"old_str": "changed", "new_str": "ALPHA"}],
            },
            {
                "path": "/mnt/user-data/workspace/b.txt",
                "expected_sha256": _sha("beta\n"),
                "replacements": [{"old_str": "beta", "new_str": "BETA"}],
            },
        ],
    )

    assert result.startswith("Error: Edit 1 version mismatch")
    assert a.read_text(encoding="utf-8") == "changed\n"
    assert b.read_text(encoding="utf-8") == "beta\n"

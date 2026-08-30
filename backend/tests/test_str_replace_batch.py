"""Atomic batch behaviour for the ``str_replace`` sandbox tool."""

from pathlib import Path
from types import SimpleNamespace

from deerflow.sandbox.local.local_sandbox import LocalSandbox
from deerflow.sandbox.tools import str_replace_tool


def _runtime(tmp_path: Path) -> SimpleNamespace:
    for subdir in ("workspace", "uploads", "outputs"):
        (tmp_path / subdir).mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        state={
            "sandbox": {"sandbox_id": "local:batch-test"},
            "thread_data": {
                "workspace_path": str(tmp_path / "workspace"),
                "uploads_path": str(tmp_path / "uploads"),
                "outputs_path": str(tmp_path / "outputs"),
            },
        },
        context={"thread_id": "batch-test"},
    )


def _invoke(tmp_path: Path, monkeypatch, content: str, **kwargs) -> tuple[str, str]:
    runtime = _runtime(tmp_path)
    target = tmp_path / "outputs" / "extension.js"
    target.write_text(content, encoding="utf-8")
    monkeypatch.setattr("deerflow.sandbox.tools.ensure_sandbox_initialized", lambda runtime: LocalSandbox("batch-test"))
    monkeypatch.setattr("deerflow.sandbox.tools.ensure_thread_directories_exist", lambda runtime: None)
    result = str_replace_tool.func(
        runtime=runtime,
        description="apply the frozen edit plan",
        path="/mnt/user-data/outputs/extension.js",
        **kwargs,
    )
    return result, target.read_text(encoding="utf-8")


def test_batch_applies_ordered_replacements_with_one_write(tmp_path, monkeypatch) -> None:
    result, content = _invoke(
        tmp_path,
        monkeypatch,
        "const mode = 'old';\nconst label = 'Old';\n",
        replacements=[
            {"old_str": "'old'", "new_str": "'new'"},
            {"old_str": "Old", "new_str": "New"},
        ],
    )

    assert result == "OK: applied 2 replacements"
    assert content == "const mode = 'new';\nconst label = 'New';\n"


def test_batch_is_atomic_when_a_later_replacement_is_missing(tmp_path, monkeypatch) -> None:
    before = "const mode = 'old';\n"
    result, content = _invoke(
        tmp_path,
        monkeypatch,
        before,
        replacements=[
            {"old_str": "'old'", "new_str": "'new'"},
            {"old_str": "not present", "new_str": "never written"},
        ],
    )

    assert result.startswith("Error: Replacement 2 string not found in file")
    assert content == before


def test_batch_supports_per_replacement_replace_all(tmp_path, monkeypatch) -> None:
    result, content = _invoke(
        tmp_path,
        monkeypatch,
        "old old old\n",
        replacements=[{"old_str": "old", "new_str": "new", "replace_all": True}],
    )

    assert result == "OK: applied 1 replacement"
    assert content == "new new new\n"


def test_batch_rejects_mixing_legacy_and_batch_arguments(tmp_path, monkeypatch) -> None:
    before = "old\n"
    result, content = _invoke(
        tmp_path,
        monkeypatch,
        before,
        old_str="old",
        new_str="new",
        replacements=[{"old_str": "old", "new_str": "other"}],
    )

    assert result.startswith("Error: Provide either old_str/new_str or replacements")
    assert content == before


def test_legacy_single_replacement_remains_supported(tmp_path, monkeypatch) -> None:
    result, content = _invoke(tmp_path, monkeypatch, "old\n", old_str="old", new_str="new")

    assert result == "OK"
    assert content == "new\n"

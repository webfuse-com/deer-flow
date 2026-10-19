"""[argus patch #148] The one-off repair script cleans stored memory, backs up first, and is idempotent."""

import importlib.util
import json
from pathlib import Path

from deerflow.agents.memory.backends.deermem.deermem.config import DeerMemConfig
from deerflow.agents.memory.backends.deermem.deermem.core.storage import create_storage

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "argus_repair_deermem_escaping.py"
SPEC = importlib.util.spec_from_file_location("argus_repair_deermem_escaping", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
repair = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(repair)

PLAIN = "Builds the 'Augmented Web Proxy' for R&D"
TWICE = "Builds the &amp;#x27;Augmented Web Proxy&amp;#x27; for R&amp;amp;D"


def _section(summary: str = "") -> dict:
    return {"summary": summary, "updatedAt": "2026-10-18T00:00:00Z"}


def _seed(root: Path) -> None:
    storage = create_storage(DeerMemConfig(storage_path=str(root)))
    user = {"workContext": _section(TWICE), "personalContext": _section("clean"), "topOfMind": _section(), "cognitiveStyle": _section()}
    history = {"recentMonths": _section(), "earlierContext": _section(), "longTermBackground": _section()}
    assert storage.save({"user": user, "history": history, "facts": []}, None, user_id="u1")
    fact = {"id": "fact_aaaa1111", "content": TWICE, "category": "context", "confidence": 0.9, "createdAt": "2026-10-01T00:00:00Z", "source": "thread-1"}
    clean = {"id": "fact_bbbb2222", "content": "Prefers short answers", "category": "preference", "confidence": 0.8, "createdAt": "2026-10-01T00:00:00Z", "source": "thread-2"}
    assert storage.save({"user": user, "history": history, "facts": [fact, clean]}, "atlas", user_id="u1")
    storage.close()


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted((root / "users").rglob("*")) if p.is_file()}


def _lines(capsys) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]


def test_dry_run_reports_and_writes_nothing(tmp_path, capsys):
    _seed(tmp_path)
    before = _snapshot(tmp_path)
    assert repair.run(["--root", str(tmp_path)]) == 0
    lines = _lines(capsys)
    assert {(line.get("user"), line.get("agent")) for line in lines if line.get("dry_run")} == {("u1", None), ("u1", "atlas")}
    assert lines[-1]["changed_scopes"] == 2 and lines[-1]["max_depth"] == 2 and lines[-1]["apply"] is False
    assert _snapshot(tmp_path) == before
    assert not (tmp_path / "backups").exists()


def test_apply_backs_up_then_repairs_and_a_second_run_changes_nothing(tmp_path, capsys):
    _seed(tmp_path)
    before = _snapshot(tmp_path)
    storage = create_storage(DeerMemConfig(storage_path=str(tmp_path)))
    facts_before = {f["id"]: f for f in storage.reload("atlas", user_id="u1")["facts"]}
    storage.close()
    backup = tmp_path.parent / f"{tmp_path.name}-backup"
    assert repair.run(["--root", str(tmp_path), "--apply", "--backup-dir", str(backup)]) == 0
    summary = _lines(capsys)[-1]
    assert summary["changed_scopes"] == 2 and summary["failed"] == 0 and summary["backup_dir"] == str(backup)

    # The backup holds the pre-repair bytes of every file of the changed user.
    assert {str(p.relative_to(backup)): p.read_bytes() for p in sorted((backup / "users").rglob("*")) if p.is_file()} == {k: v for k, v in before.items()}

    storage = create_storage(DeerMemConfig(storage_path=str(tmp_path)))
    summaries = storage.reload(None, user_id="u1")
    facts = {f["id"]: f for f in storage.reload("atlas", user_id="u1")["facts"]}
    storage.close()
    assert summaries["user"]["workContext"]["summary"] == PLAIN
    assert summaries["user"]["personalContext"]["summary"] == "clean"
    assert facts["fact_aaaa1111"]["content"] == PLAIN
    # Every other field of the repaired fact is untouched, and the clean fact is unchanged.
    stamped = {"content", "updatedAt", "revision"}  # the storage stamps a write
    assert {k: v for k, v in facts["fact_aaaa1111"].items() if k not in stamped} == {k: v for k, v in facts_before["fact_aaaa1111"].items() if k not in stamped}
    assert facts["fact_bbbb2222"] == facts_before["fact_bbbb2222"]

    after = _snapshot(tmp_path)
    assert repair.run(["--root", str(tmp_path), "--apply", "--backup-dir", str(tmp_path.parent / f"{tmp_path.name}-backup2")]) == 0
    again = _lines(capsys)[-1]
    assert again["changed_scopes"] == 0 and "backup_dir" not in again
    assert _snapshot(tmp_path) == after
    assert not (tmp_path.parent / f"{tmp_path.name}-backup2").exists()


def test_user_filter_limits_the_scopes(tmp_path, capsys):
    _seed(tmp_path)
    assert repair.run(["--root", str(tmp_path), "--user", "someone-else"]) == 0
    assert _lines(capsys)[-1]["scopes"] == 0


def test_missing_root_is_an_error(tmp_path, capsys):
    assert repair.run(["--root", str(tmp_path / "nowhere")]) == 1
    assert _lines(capsys)[-1]["error"] == "no users directory"

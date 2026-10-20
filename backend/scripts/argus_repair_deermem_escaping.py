#!/usr/bin/env python3
"""[argus patch #148] One-off repair: undo HTML escaping that accumulated in DeerMem memory.

Before patch #148 every memory update stored the model's copy of the escaped
prompt text, so summaries and facts gained one entity level per cycle
(``'`` -> ``&#x27;`` -> ``&amp;#x27;``). Patch #148 stops new growth; this
script cleans what is already stored, through DeerMem's own storage API (the
same cross-process file lock, revision check and retrieval-index update a
gateway uses), after copying each changed user's memory tree to a backup.

Where memory lives: ``{storage_path}/users/{user_id}/memory.json`` holds the
user and history summaries; ``{storage_path}/users/{user_id}/agents/{agent}/
facts/{xx}/{fact_id}.md`` holds the facts; ``{storage_path}/.retrieval`` is the
derived search index. ``storage_path`` defaults to the gateway's DEER_FLOW_HOME
(``/app/backend/.deer-flow`` in an Argus gateway, the host's
``projects/<stack>/data``).

Run it inside the stack's gateway container, only on an image that carries
patch #148 (the script refuses otherwise), dry run first:

    podman exec argus-atlas-<name>-gateway sh -c \\
      'cd /app/backend && PYTHONPATH=. .venv/bin/python scripts/argus_repair_deermem_escaping.py'
    podman exec argus-atlas-<name>-gateway sh -c \\
      'cd /app/backend && PYTHONPATH=. .venv/bin/python scripts/argus_repair_deermem_escaping.py --apply'

``--apply`` writes backups to ``{storage_path}/backups/deermem-unescape-<UTC>/``
first (override with ``--backup-dir``) and prints one JSON line per scope.
Restore a user by copying ``<backup>/users/<user_id>`` back over
``{storage_path}/users/<user_id>`` while the gateway is idle.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SUMMARY_GROUPS = ("user", "history")
FACT_TEXT_FIELDS = ("content", "category", "sourceError")


_MAX_ROUNDS = 16


def _load_unescape():
    """The repair's unescape: the updater's one round, repeated until the text stops changing.

    Only this one-off repair goes to a fixed point. The updater undoes exactly
    the one level its prompt adds, so a literal entity in a real memory
    survives every update; here the accumulated levels are what is cleaned.
    """
    try:
        from deerflow.agents.memory.backends.deermem.deermem.core.updater import _unescape_prompt_text
    except ImportError:
        sys.exit("This DeerFlow build does not carry patch #148; repairing now would let the escaping grow back. Deploy the fix first.")

    def unescape_fully(text: str) -> str:
        current = text
        for _ in range(_MAX_ROUNDS):
            step = _unescape_prompt_text(current)
            if step == current:
                break
            current = step
        return current

    unescape_fully.one_round = _unescape_prompt_text  # type: ignore[attr-defined]
    return unescape_fully


def _depth(text: str, unescape) -> int:
    """How many unescape rounds the text needs (0 = clean)."""
    one_round = getattr(unescape, "one_round", unescape)
    rounds = 0
    current = text
    while rounds < _MAX_ROUNDS:
        step = one_round(current)
        if step == current:
            break
        current = step
        rounds += 1
    return rounds


def _storage_config(args: argparse.Namespace):
    from deerflow.agents.memory.backends.deermem.deermem.config import DeerMemConfig

    backend_config: dict[str, Any] = {}
    if args.root is None:
        try:
            from deerflow.agents.memory.manager import resolve_deermem_storage_path
            from deerflow.config.app_config import get_app_config
            from deerflow.config.memory_config import get_memory_config

            get_app_config()  # loads config.yaml, which sets the memory section
            backend_config = dict(get_memory_config().backend_config or {})
            backend_config["storage_path"] = resolve_deermem_storage_path(backend_config)
        except Exception as exc:  # noqa: BLE001
            sys.exit(f"Could not read the gateway's memory config ({exc}); pass --root explicitly.")
    else:
        backend_config["storage_path"] = str(Path(args.root).resolve())
    keep = {"storage_path", "storage_class", "strict_user_scope", "manifest_filename", "file_lock_timeout_seconds", "retrieval_adapter", "retrieval_index_path"}
    return DeerMemConfig.from_backend_config({key: value for key, value in backend_config.items() if key in keep})


def _clean_document(document: dict[str, Any], *, summaries: bool, facts: bool, unescape) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return (cleaned copy, list of changes) for one storage scope."""
    cleaned = copy.deepcopy(document)
    changes: list[dict[str, Any]] = []
    if summaries:
        for group in SUMMARY_GROUPS:
            sections = cleaned.get(group)
            if not isinstance(sections, dict):
                continue
            for name, section in sections.items():
                if isinstance(section, dict) and isinstance(section.get("summary"), str):
                    fixed = unescape(section["summary"])
                    if fixed != section["summary"]:
                        changes.append({"where": f"{group}.{name}", "depth": _depth(section["summary"], unescape)})
                        section["summary"] = fixed
    if facts:
        for fact in cleaned.get("facts") or []:
            if not isinstance(fact, dict):
                continue
            for field in FACT_TEXT_FIELDS:
                value = fact.get(field)
                if isinstance(value, str):
                    fixed = unescape(value)
                    if fixed != value:
                        changes.append({"where": f"fact {fact.get('id')}.{field}", "depth": _depth(value, unescape)})
                        fact[field] = fixed
    return cleaned, changes


def _scopes(root: Path) -> list[tuple[str, str | None]]:
    """Every (user_id, agent_name) scope with data; agent None = the user's summaries."""
    scopes: list[tuple[str, str | None]] = []
    users = root / "users"
    if not users.is_dir():
        return scopes
    for user_dir in sorted(p for p in users.iterdir() if p.is_dir()):
        scopes.append((user_dir.name, None))
        agents = user_dir / "agents"
        if agents.is_dir():
            for agent_dir in sorted(p for p in agents.iterdir() if p.is_dir() and (p / "facts").is_dir()):
                scopes.append((user_dir.name, agent_dir.name))
    return scopes


def _backup_user(root: Path, backup_dir: Path, user_id: str, done: set[str]) -> None:
    if user_id in done:
        return
    source = root / "users" / user_id
    target = backup_dir / "users" / user_id
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, symlinks=True)
    done.add(user_id)


def _repair_scope(storage, root: Path, backup_dir: Path, backed_up: set[str], user_id: str, agent_name: str | None, *, apply: bool, unescape, conflict) -> dict[str, Any] | None:
    """Clean one scope; None when it is already clean. A concurrent gateway write is re-read once."""
    for attempt in range(2):
        document = storage.load(agent_name, user_id=user_id) if attempt == 0 else storage.reload(agent_name, user_id=user_id)
        cleaned, changes = _clean_document(document, summaries=agent_name is None, facts=agent_name is not None, unescape=unescape)
        if not changes:
            return None
        line: dict[str, Any] = {"user": user_id, "agent": agent_name, "changes": len(changes), "max_depth": max(c["depth"] for c in changes), "fields": [c["where"] for c in changes][:20]}
        if not apply:
            line["dry_run"] = True
            return line
        _backup_user(root, backup_dir, user_id, backed_up)
        payload = cleaned if agent_name is not None else {**cleaned, "facts": []}
        try:
            line["saved"] = bool(storage.save(payload, agent_name, user_id=user_id, expected_revision=int(document.get("revision") or 0)))
        except conflict:
            if attempt == 0:
                continue
            line["saved"] = False
            line["error"] = "revision conflict twice"
        return line
    return None


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write the repair (default: dry run)")
    parser.add_argument("--root", help="DeerMem storage_path (default: from the gateway's memory config)")
    parser.add_argument("--backup-dir", help="where --apply copies each changed user's tree first")
    parser.add_argument("--user", action="append", help="limit to these user ids (repeatable)")
    args = parser.parse_args(argv)

    unescape = _load_unescape()
    from deerflow.agents.memory.backends.deermem.deermem.core.paths import validate_agent_name
    from deerflow.agents.memory.backends.deermem.deermem.core.storage import MemoryRevisionConflict, create_storage

    config = _storage_config(args)
    root = Path(config.storage_path)
    if not (root / "users").is_dir():
        print(json.dumps({"root": str(root), "error": "no users directory"}))
        return 1
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = Path(args.backup_dir) if args.backup_dir else root / "backups" / f"deermem-unescape-{stamp}"
    storage = create_storage(config)
    backed_up: set[str] = set()
    totals = {"scopes": 0, "changed_scopes": 0, "changes": 0, "max_depth": 0, "failed": 0}
    try:
        for user_id, agent_name in _scopes(root):
            if args.user and user_id not in args.user:
                continue
            if agent_name is not None:
                try:
                    validate_agent_name(agent_name)
                except ValueError:
                    print(json.dumps({"user": user_id, "agent": agent_name, "skipped": "invalid agent directory name"}))
                    continue
            totals["scopes"] += 1
            try:
                line = _repair_scope(storage, root, backup_dir, backed_up, user_id, agent_name, apply=args.apply, unescape=unescape, conflict=MemoryRevisionConflict)
            except Exception as exc:  # noqa: BLE001 - one bad scope must not stop the others
                print(json.dumps({"user": user_id, "agent": agent_name, "saved": False, "error": f"{type(exc).__name__}: {exc}"[:300]}))
                totals["failed"] += 1
                continue
            if line is None:
                continue
            print(json.dumps(line))
            totals["changed_scopes"] += 1
            totals["changes"] += line["changes"]
            totals["max_depth"] = max(totals["max_depth"], line["max_depth"])
            if args.apply and not line.get("saved"):
                totals["failed"] += 1
    finally:
        close = getattr(storage, "close", None)
        if close is not None:
            close()
    summary = {"root": str(root), "apply": args.apply, **totals}
    if args.apply and backed_up:
        summary["backup_dir"] = str(backup_dir)
    print(json.dumps(summary))
    return 1 if totals["failed"] else 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    raise SystemExit(run())

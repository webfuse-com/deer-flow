"""Shared content-signature helper for runtime-editable config files.

Both ``config/app_config.py`` (``config.yaml``) and ``mcp/cache.py``
(``extensions_config.json``) need to detect when a runtime-editable config
file has actually changed, even under conditions a bare mtime comparison
misses: same-second edits, mtime that stays put or moves backward
(``git checkout``, ``cp -p`` / backup restore, ``tar`` / ``rsync`` that
preserve timestamps, object-store / network mounts), or a switch to a
different file whose mtime is <= the previously recorded one.

This module is the single implementation of that ``(mtime, size, sha256)``
signature so the two call sites share one behavior instead of maintaining
verbatim-duplicate copies that can silently drift apart over time.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

# (mtime, size, sha256-hexdigest) recorded for a config file, or the current
# values recomputed for comparison against a previously recorded one. A
# ``None`` digest (third element) means the stat succeeded but the content
# could not be read; the whole tuple is ``None`` when the file could not be
# stat-ed at all (e.g. it does not exist).
ConfigSignature = tuple[float | None, int | None, str | None]


def get_config_signature(config_path: Path) -> ConfigSignature | None:
    """Get cache metadata for *config_path*, including a content digest.

    Returns ``None`` when the file cannot be stat-ed (e.g. it does not
    exist), so callers can treat "no file" as a distinct case from "file
    with unreadable content" (which still yields a partial signature below).
    """
    try:
        stat_result = config_path.stat()
    except OSError:
        return None

    # Always hash the full file here rather than short-circuiting when
    # mtime/size already match a previously recorded signature: swapping in
    # different content of identical byte length within the same second
    # leaves mtime *and* size unchanged, so only the sha256 catches that
    # swap. Skipping the hash on an mtime/size match would reopen the narrow
    # gap this signature was built to close.
    digest = hashlib.sha256()
    try:
        with config_path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return (stat_result.st_mtime, stat_result.st_size, None)

    return (stat_result.st_mtime, stat_result.st_size, digest.hexdigest())


def signatures_differ(old: ConfigSignature | None, new: ConfigSignature | None) -> bool:
    """Return True when two config signatures represent *different file content*.

    This is the staleness predicate both runtime-editable config files use. It
    deliberately ignores ``mtime`` and ``size`` and compares only the sha256
    content digest. A byte-identical rewrite — ``git checkout`` / ``git reset
    --hard`` touching a file to the same content, ``cp -p`` / ``rsync`` /
    backup restore, a filesystem remount that bumps atime/mtime — must NOT be
    treated as a change: invalidating on those fires a full config reload and
    (for the MCP cache) a synchronous re-discovery of every MCP server, which
    is exactly the stall seen when a fork-sync daemon resets a checkout to an
    identical commit right after a run's answer finished generating.

    Both ``None`` (file could not be stat-ed) and a ``None`` digest (stat
    succeeded but content unreadable) are treated as "no change": the callers
    (``mcp.cache`` and ``app_config``) intentionally fail soft and keep serving
    the last-known-good configuration rather than tearing down on a transient
    unreadable file.
    """
    if old is None or new is None:
        return False
    old_digest = old[2]
    new_digest = new[2]
    if old_digest is None or new_digest is None:
        return False
    return old_digest != new_digest

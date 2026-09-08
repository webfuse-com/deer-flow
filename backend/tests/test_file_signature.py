"""Unit tests for the shared config-file content-signature helper.

``deerflow.config.file_signature.get_config_signature`` was extracted from
verbatim-duplicate implementations that used to live independently in
``deerflow.config.app_config`` and ``deerflow.mcp.cache`` (flagged in review
on PR #4124: "now a verbatim duplicate of
``deerflow/config/app_config.py::_get_config_signature`` / ``_ConfigSignature``
... worth a follow-up to extract both into a small shared helper"). These
tests cover the shared implementation directly, and pin that both former
call sites now delegate to it instead of maintaining independent copies that
can silently drift apart.
"""

from __future__ import annotations

import os
from pathlib import Path

from deerflow.config.file_signature import ConfigSignature, get_config_signature


def test_missing_file_returns_none(tmp_path: Path):
    missing = tmp_path / "does-not-exist.json"
    assert get_config_signature(missing) is None


def test_existing_file_returns_full_signature(tmp_path: Path):
    cfg = tmp_path / "config.json"
    cfg.write_text('{"a": 1}', encoding="utf-8")

    signature = get_config_signature(cfg)

    assert signature is not None
    mtime, size, digest = signature
    assert mtime == cfg.stat().st_mtime
    assert size == cfg.stat().st_size
    assert isinstance(digest, str) and len(digest) == 64  # sha256 hexdigest


def test_content_change_changes_signature_even_with_same_mtime_and_size(tmp_path: Path):
    """The digest -- not just mtime/size -- must catch a same-length content
    swap within the same second (the exact hole the sha256 exists to close)."""
    cfg = tmp_path / "config.json"
    cfg.write_text('{"server": "srv1"}', encoding="utf-8")
    before = get_config_signature(cfg)
    assert before is not None
    recorded_mtime, recorded_size = before[0], before[1]

    cfg.write_text('{"server": "srv9"}', encoding="utf-8")  # same length, different content
    os.utime(cfg, (recorded_mtime, recorded_mtime))
    assert cfg.stat().st_mtime == recorded_mtime  # guard: mtime truly unchanged
    assert cfg.stat().st_size == recorded_size  # guard: size truly unchanged too

    after = get_config_signature(cfg)
    assert after is not None
    assert after[0] == before[0]
    assert after[1] == before[1]
    assert after[2] != before[2]  # only the digest catches the swap


def test_noop_rewrite_same_content_is_not_a_change(tmp_path: Path):
    """A byte-identical rewrite (fork-sync ``git reset --hard`` to the same
    commit, a remount, ``cp -p``) bumps mtime but leaves the sha256 identical.
    ``signatures_differ`` must report NO change, or the MCP cache and AppConfig
    reload fire a full synchronous re-discovery inside a run's completion
    path — the multi-second post-answer stall this guards against."""
    from deerflow.config.file_signature import signatures_differ

    cfg = tmp_path / "config.yaml"
    cfg.write_text("models: []\n", encoding="utf-8")
    before = get_config_signature(cfg)
    assert before is not None

    # Rewrite identical content and force a DIFFERENT mtime (newer, then older).
    cfg.write_text("models: []\n", encoding="utf-8")
    later = cfg.stat().st_mtime + 100
    os.utime(cfg, (later, later))
    newer_mtime_same_content = get_config_signature(cfg)
    assert newer_mtime_same_content is not None
    assert newer_mtime_same_content[0] != before[0]  # guard: mtime really changed

    assert signatures_differ(before, newer_mtime_same_content) is False


def test_signatures_differ_ignores_mtime_and_size(tmp_path: Path):
    """Direct unit contract: only the sha256 digest decides, never mtime/size."""
    from deerflow.config.file_signature import signatures_differ

    cfg = tmp_path / "config.yaml"
    cfg.write_text("alpha: 1\n", encoding="utf-8")
    a = get_config_signature(cfg)
    cfg.write_text("beta: 2\n", encoding="utf-8")
    b = get_config_signature(cfg)
    assert a is not None and b is not None

    # Same digest, wildly different mtime/size -> not a change.
    same_digest_newer = (a[0] + 9999, a[1], a[2])
    assert signatures_differ(a, same_digest_newer) is False
    # Different digest -> a change (regardless of mtime/size agreement).
    assert signatures_differ(a, b) is True
    # None / missing-digest cases fail soft (treated as no change).
    assert signatures_differ(None, b) is False
    assert signatures_differ(a, None) is False
    assert signatures_differ((a[0], a[1], None), b) is False
    assert signatures_differ(a, (b[0], b[1], None)) is False


def test_signature_type_alias_shape():
    """ConfigSignature is the (mtime, size, sha256) tuple type both call sites share."""
    assert ConfigSignature == tuple[float | None, int | None, str | None]


def test_app_config_and_mcp_cache_share_the_same_implementation():
    """Regression guard for the PR #4124 review finding: both modules must
    delegate to this shared helper rather than maintaining independent
    verbatim copies that can silently drift apart over time.
    """
    import deerflow.config.app_config as app_config_module
    import deerflow.mcp.cache as cache_module

    assert app_config_module._get_config_signature is get_config_signature
    assert cache_module._get_config_signature is get_config_signature
    assert app_config_module._ConfigSignature is ConfigSignature
    assert cache_module._ConfigSignature is ConfigSignature

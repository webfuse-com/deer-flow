"""[argus] Tests for the channel-aware artifact presenter (fork patch #10)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.channels._artifact_presenter import present_artifacts
from app.channels.message_bus import ResolvedAttachment


def _att(vpath: str, mime: str) -> ResolvedAttachment:
    name = vpath.rsplit("/", 1)[-1]
    return ResolvedAttachment(
        virtual_path=vpath,
        actual_path=Path("/tmp") / name,
        filename=name,
        mime_type=mime,
        size=10,
        is_image=mime.startswith("image/"),
    )


@pytest.fixture(autouse=True)
def _host(monkeypatch):
    monkeypatch.setenv("ATLAS_PUBLIC_HOST", "atlas-nicholas.acro.surfly.com")


def test_telegram_html_is_link_only():
    vpath = "/mnt/user-data/outputs/report.html"
    text, keep = present_artifacts(
        "telegram", "thread-1", [vpath], [_att(vpath, "text/html")]
    )
    assert "https://atlas-nicholas.acro.surfly.com/f/thread-1/report.html" in text
    assert "[report.html]" in text
    # HTML artifact is NOT re-attached — the link is the presentation.
    assert keep == []


def test_telegram_binary_is_link_and_attached():
    vpath = "/mnt/user-data/outputs/data.pdf"
    att = _att(vpath, "application/pdf")
    text, keep = present_artifacts("telegram", "t2", [vpath], [att])
    assert "/f/t2/data.pdf" in text
    assert keep == [att]  # pdf still attached


def test_telegram_nested_path_link():
    vpath = "/mnt/user-data/outputs/sub dir/page.html"
    text, _ = present_artifacts("telegram", "t3", [vpath], [_att(vpath, "text/html")])
    # Spaces are percent-encoded, slashes preserved.
    assert "/f/t3/sub%20dir/page.html" in text


def test_non_telegram_falls_back():
    vpath = "/mnt/user-data/outputs/report.html"
    att = _att(vpath, "text/html")
    text, keep = present_artifacts("slack", "t4", [vpath], [att])
    assert "Created File:" in text and "report.html" in text
    assert keep == [att]  # unchanged for other channels


def test_no_host_falls_back(monkeypatch):
    monkeypatch.delenv("ATLAS_PUBLIC_HOST", raising=False)
    monkeypatch.delenv("ARGUS_PROJECT", raising=False)
    vpath = "/mnt/user-data/outputs/report.html"
    att = _att(vpath, "text/html")
    text, keep = present_artifacts("telegram", "t5", [vpath], [att])
    assert "Created File:" in text
    assert keep == [att]


def test_host_derived_from_project(monkeypatch):
    monkeypatch.delenv("ATLAS_PUBLIC_HOST", raising=False)
    vpath = "/mnt/user-data/outputs/r.html"
    text, _ = present_artifacts(
        "telegram", "t6", [vpath], [_att(vpath, "text/html")], project="atlas-thomas"
    )
    assert "https://atlas-thomas.acro.surfly.com/f/t6/r.html" in text


def test_empty_artifacts():
    text, keep = present_artifacts("telegram", "t7", [], [])
    assert text == "" and keep == []


def test_multiple_files_header():
    v1 = "/mnt/user-data/outputs/a.html"
    v2 = "/mnt/user-data/outputs/b.pdf"
    text, keep = present_artifacts(
        "telegram", "t8", [v1, v2], [_att(v1, "text/html"), _att(v2, "application/pdf")]
    )
    assert text.startswith("Files ready:")
    assert "/f/t8/a.html" in text and "/f/t8/b.pdf" in text
    # Only the pdf is kept as an attachment.
    assert len(keep) == 1 and keep[0].filename == "b.pdf"

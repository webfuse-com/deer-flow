"""[argus] Channel-aware artifact presentation.

When the agent calls present_files, the manager resolves the paths to
ResolvedAttachments and, by default, attaches the raw files + a "Created
File: 📎 …" text block (see manager.py _format_artifact_text).

For Telegram that's a poor fit: an HTML report (often multi-file, with its own
CSS/JS) arrives as a download that can't render in-chat. Instead we hand back a
VIEWABLE link to the per-stack fileserver (the `/f/` route on the stack's own
nginx, behind the Caddy edge + Google SSO), where the report renders in the
browser with its relative assets intact.

This is a deterministic interception keyed on the target channel — it fires on
every present_files, with no dependence on the model formatting a URL itself.
The manager is the right seam because it (unlike graph middleware) knows which
channel the OutboundMessage targets.

Carried as part of Argus fork patch #10 — see PATCHES.md.
"""

from __future__ import annotations

import logging
import os
import posixpath
import re

from app.channels.message_bus import ResolvedAttachment

logger = logging.getLogger(__name__)

# A code block longer than this is treated as a file dump (an agent pasting an
# SVG/HTML it also wrote to outputs). When we're presenting a viewable link to
# such a file, the inline dump is redundant noise in chat, so we strip it.
_INLINE_DUMP_MIN = 600

# Fenced markdown code blocks and pre-converted HTML <pre> blocks.
_FENCED_RE = re.compile(r"```[\w-]*\n.*?```", re.DOTALL)
_PRE_RE = re.compile(r"<pre>(?:<code>)?.*?(?:</code>)?</pre>", re.DOTALL)


def strip_inlined_artifacts(text: str, attachments: list[ResolvedAttachment]) -> str:
    """Remove oversized inline code/<pre> dumps from the chat text when we're
    presenting the corresponding file as a link. Agents sometimes write a file
    AND paste its full source; once we hand back a /f/ link the wall of source
    is just noise. Conservative: only strips blocks above _INLINE_DUMP_MIN
    chars, and only when at least one web/text artifact is being linked."""
    if not text or not attachments:
        return text
    has_textual = any(
        a.mime_type in _WEB_VIEWABLE or a.mime_type.startswith("text/") or a.mime_type == "application/json"
        for a in attachments
    )
    if not has_textual:
        return text

    def _drop(m: re.Match) -> str:
        return "" if len(m.group(0)) >= _INLINE_DUMP_MIN else m.group(0)

    text = _FENCED_RE.sub(_drop, text)
    text = _PRE_RE.sub(_drop, text)
    # Collapse the blank lines a removed block leaves behind.
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text

_OUTPUTS_VIRTUAL_PREFIX = "/mnt/user-data/outputs/"

# MIME types that render usefully in a browser via the /f/ link (and whose raw
# file we therefore SUPPRESS as an attachment — the link is the presentation).
# Everything else (pdf, png, csv, zip, …) is still attached AND linked.
_WEB_VIEWABLE = {
    "text/html",
    "application/xhtml+xml",
    "image/svg+xml",
}


def _public_host(project: str | None) -> str | None:
    """Resolve the stack's public hostname for building /f/ links.

    Priority:
      1. ATLAS_PUBLIC_HOST env (explicit override, e.g. set per stack).
      2. <project>.<ATLAS_PUBLIC_DOMAIN> derived from ARGUS_PROJECT.
         ATLAS_PUBLIC_DOMAIN defaults to acro.surfly.com.
    Returns None if we can't determine a host (then we don't build links).
    """
    explicit = os.getenv("ATLAS_PUBLIC_HOST", "").strip()
    if explicit:
        return explicit
    proj = (project or os.getenv("ARGUS_PROJECT", "")).strip()
    if not proj:
        return None
    domain = os.getenv("ATLAS_PUBLIC_DOMAIN", "acro.surfly.com").strip()
    return f"{proj}.{domain}"


def _relpath(virtual_path: str) -> str | None:
    """Map a /mnt/user-data/outputs/<rel> virtual path to its <rel> tail."""
    if not virtual_path.startswith(_OUTPUTS_VIRTUAL_PREFIX):
        return None
    return virtual_path[len(_OUTPUTS_VIRTUAL_PREFIX):].lstrip("/")


def _file_url(host: str, thread_id: str, relpath: str) -> str:
    # The /f/ nginx route maps /f/<thread_id>/<rel> → that thread's outputs dir.
    # quote each segment but keep the slashes.
    from urllib.parse import quote

    safe_rel = "/".join(quote(seg, safe="") for seg in relpath.split("/"))
    return f"https://{host}/f/{quote(thread_id, safe='')}/{safe_rel}"


def present_artifacts(
    channel_name: str,
    thread_id: str,
    artifacts: list[str],
    attachments: list[ResolvedAttachment],
    *,
    project: str | None = None,
) -> tuple[str, list[ResolvedAttachment]]:
    """Produce the channel-appropriate (text_block, attachments_to_send).

    - telegram: append markdown links to the per-stack fileserver. Web-viewable
      artifacts (HTML/SVG) are presented as links ONLY (their raw file is
      suppressed from the upload set); other binaries are linked AND attached.
      The text block is markdown — the Telegram channel's HTML formatter turns
      "[name](url)" into a native link.
    - anything else: fall back to the default presentation (filename block +
      attach everything), so no other channel regresses.

    Returns an empty text block + the original attachments if there are no
    artifacts, or if we can't resolve a public host (link-less degrade).
    """
    if not artifacts:
        return "", attachments

    if channel_name != "telegram":
        return _default_text(artifacts), attachments

    host = _public_host(project)
    if not host:
        logger.warning("[present] no public host for project=%s; falling back to attachments", project)
        return _default_text(artifacts), attachments

    # Index resolved attachments by virtual_path so we can decide per-file
    # whether to keep attaching it.
    by_vpath = {a.virtual_path: a for a in attachments}

    lines: list[str] = []
    keep: list[ResolvedAttachment] = []
    for vpath in artifacts:
        rel = _relpath(vpath)
        name = posixpath.basename(vpath)
        att = by_vpath.get(vpath)
        if rel is None:
            # Non-outputs artifact (shouldn't reach here — manager filters — but
            # be defensive): just keep whatever attachment exists.
            if att:
                keep.append(att)
            continue
        url = _file_url(host, thread_id, rel)
        mime = att.mime_type if att else ""
        if mime in _WEB_VIEWABLE:
            # Link only — the rendered page IS the presentation.
            lines.append(f"📄 [{name}]({url})")
        else:
            # Link AND attach (pdf/png/csv/etc — useful to have in-chat too).
            lines.append(f"📎 [{name}]({url})")
            if att:
                keep.append(att)

    if not lines:
        return _default_text(artifacts), attachments

    header = "Report ready:" if len(lines) == 1 else "Files ready:"
    return header + "\n" + "\n".join(lines), keep


def _default_text(artifacts: list[str]) -> str:
    """Mirror manager._format_artifact_text for the non-telegram fallback."""
    filenames = [posixpath.basename(p) for p in artifacts]
    if len(filenames) == 1:
        return f"Created File: 📎 {filenames[0]}"
    return "Created Files: 📎 " + "、".join(filenames)

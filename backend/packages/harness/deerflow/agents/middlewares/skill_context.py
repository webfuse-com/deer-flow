"""Deterministic capture and rendering for loaded skill files."""

from __future__ import annotations

import logging
import posixpath
import re
from collections.abc import Collection, Mapping
from html import escape, unescape
from typing import Any, TypedDict

import yaml
from langchain_core.messages import AIMessage, AnyMessage, ToolMessage

from deerflow.agents.thread_state import _SKILL_DESCRIPTION_MAX_CHARS, SkillEntry

_SKILL_FILE_NAME = "SKILL.md"
# `\ufeff` is the UTF-8 byte-order mark a Windows-saved SKILL.md can carry. The loader and
# validator already tolerate it (see `deerflow.skills.frontmatter._FRONTMATTER_RE`); without it
# here the match fails on the leading marker and the captured description comes back empty.
_FRONT_MATTER_RE = re.compile(r"^\ufeff?---\s*\n(.*?)\n---\s*\n", re.DOTALL)
SKILL_CONTEXT_ENTRY_KEY = "skill_context_entry"
# Stamped instead of SKILL_CONTEXT_ENTRY_KEY when the read SKILL.md belongs to
# a skill whose action-scoped ``skill:activate`` decision is denied: the file
# content was served (Layer 1 visibility allows it) but the skill must not
# activate — no durable skill_context entry, tool policy, or secret binding.
# extract_skills uses it to skip the entry without the "missing skill read
# metadata" warning that an unstamped read would otherwise produce.
SKILL_CONTEXT_DENIED_KEY = "skill_context_denied"
logger = logging.getLogger(__name__)


class SkillEntryMetadata(TypedDict):
    path: str
    description: str


def _tool_call_name(tool_call: dict[str, Any]) -> str:
    name = tool_call.get("name")
    if isinstance(name, str):
        return name
    function = tool_call.get("function")
    if isinstance(function, dict) and isinstance(function.get("name"), str):
        return function["name"]
    return ""


def _tool_call_id(tool_call: dict[str, Any]) -> str | None:
    tool_call_id = tool_call.get("id")
    return str(tool_call_id) if tool_call_id else None


def _tool_call_path(tool_call: dict[str, Any]) -> str | None:
    args = tool_call.get("args")
    if not isinstance(args, dict):
        return None
    for key in ("path", "file_path", "filepath"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _normalize_under_root(path: str, normalized_root: str) -> str | None:
    normalized = posixpath.normpath(path)
    if normalized == normalized_root or normalized.startswith(normalized_root + "/"):
        return normalized
    return None


def _is_skill_file(path: str) -> bool:
    return posixpath.basename(path) == _SKILL_FILE_NAME


def _skill_name_from_path(skill_md_path: str) -> str:
    """Derive the skill name from the directory containing SKILL.md."""
    return posixpath.basename(posixpath.dirname(skill_md_path))


def _parse_description(content: str) -> str:
    """Extract frontmatter description from already-read SKILL.md content."""
    match = _FRONT_MATTER_RE.match(content)
    if not match:
        return ""
    try:
        metadata = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return ""
    if not isinstance(metadata, dict):
        return ""
    description = metadata.get("description")
    if not isinstance(description, str):
        return ""
    return " ".join(description.split())[:_SKILL_DESCRIPTION_MAX_CHARS]


def _is_tool_error_text(content: str) -> bool:
    return content.lstrip().startswith("Error:")


def build_skill_entry_metadata_from_read(
    path: str,
    content: str,
    *,
    skills_root: str,
) -> SkillEntryMetadata | None:
    normalized_root = posixpath.normpath(skills_root.rstrip("/") or "/")
    normalized_path = _normalize_under_root(path, normalized_root)
    if normalized_path is None or not _is_skill_file(normalized_path) or _is_tool_error_text(content):
        return None
    return {
        "path": normalized_path,
        "description": _parse_description(content),
    }


def read_skill_entry_metadata(additional_kwargs: Mapping[str, object] | None) -> SkillEntryMetadata | None:
    if not additional_kwargs:
        return None
    raw = additional_kwargs.get(SKILL_CONTEXT_ENTRY_KEY)
    if not isinstance(raw, Mapping):
        return None
    path = raw.get("path")
    description = raw.get("description")
    if not isinstance(path, str):
        return None
    return {
        "path": path,
        "description": " ".join(description.split())[:_SKILL_DESCRIPTION_MAX_CHARS] if isinstance(description, str) else "",
    }


def _escape_context_text(value: object) -> str:
    return escape(str(value), quote=False)


def extract_skills(
    messages: list[AnyMessage],
    *,
    skills_root: str,
    read_tool_names: Collection[str],
) -> list[SkillEntry]:
    """Enumerate skill-file reads (AI read_file call + paired ToolMessage result)."""
    normalized_root = posixpath.normpath(skills_root.rstrip("/") or "/")
    read_names = frozenset(read_tool_names)

    skill_paths_by_id: dict[str, str] = {}
    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        for tool_call in message.tool_calls or []:
            if _tool_call_name(tool_call) not in read_names:
                continue
            tool_call_id = _tool_call_id(tool_call)
            raw_path = _tool_call_path(tool_call)
            path = _normalize_under_root(raw_path, normalized_root) if raw_path else None
            if tool_call_id and path and _is_skill_file(path):
                skill_paths_by_id[tool_call_id] = path

    entries: list[SkillEntry] = []
    for index, message in enumerate(messages):
        if not isinstance(message, ToolMessage):
            continue
        if getattr(message, "status", "success") == "error":
            continue
        tool_call_id = str(message.tool_call_id) if message.tool_call_id else ""
        expected_path = skill_paths_by_id.get(tool_call_id)
        if expected_path is None:
            continue
        if (message.additional_kwargs or {}).get(SKILL_CONTEXT_DENIED_KEY):
            # The skill:activate decision denied this read's skill — the
            # middleware stamped the denial instead of entry metadata on
            # purpose, so this is not the "missing metadata" bug shape.
            continue
        metadata = read_skill_entry_metadata(message.additional_kwargs)
        if metadata is None:
            content = message.content if isinstance(message.content, str) else ""
            if not _is_tool_error_text(content):
                logger.warning("missing skill read metadata: tool_call_id=%s path=%s", tool_call_id, expected_path)
            continue
        if metadata["path"] != expected_path:
            logger.warning(
                "mismatched skill read metadata: tool_call_id=%s expected_path=%s metadata_path=%s",
                tool_call_id,
                expected_path,
                metadata["path"],
            )
            continue
        entries.append(
            {
                "name": _skill_name_from_path(expected_path),
                "path": expected_path,
                "description": metadata["description"],
                "loaded_at": index,
            }
        )
    return entries


def _render_skill_line(entry: SkillEntry) -> str:
    name = _escape_context_text(entry["name"])
    path = _escape_context_text(entry["path"])
    raw_description = entry.get("description") or ""
    if isinstance(raw_description, str):
        raw_description = " ".join(raw_description.split())[:_SKILL_DESCRIPTION_MAX_CHARS]
    description = _escape_context_text(raw_description)
    suffix = f": {description}" if description else ""
    return f"- {name}{suffix} -> {path}"


def render_skill_context(entries: list[SkillEntry], *, in_context_paths: Collection[str] | None = None) -> str:
    """Render active-skill references as a compact reminder, not the body.

    [argus patch #145] With ``in_context_paths`` (``skills.reread_only_after_summary``)
    only skills whose body is no longer in the conversation are asked to be
    re-read; the upstream header made models re-read skills they still held
    (atlas-salome run 4f37f936: three extra turns of about 40k tokens each).
    """
    if not entries:
        return ""

    if in_context_paths is None:
        lines = ["## Active skills (loaded earlier - re-read the file before applying its instructions)"]
        lines.extend(_render_skill_line(entry) for entry in entries)
        return "\n".join(lines)

    present = {posixpath.normpath(path) for path in in_context_paths}
    held = [entry for entry in entries if posixpath.normpath(str(entry.get("path") or "")) in present]
    dropped = [entry for entry in entries if entry not in held]
    lines = []
    if held:
        lines.append("## Active skills (their instructions are above in this conversation; follow them without re-reading)")
        lines.extend(_render_skill_line(entry) for entry in held)
    if dropped:
        lines.append("## Active skills summarized away (re-read the file before applying its instructions)")
        lines.extend(_render_skill_line(entry) for entry in dropped)
    return "\n".join(lines)


_ROUTED_SKILL_PATH_RE = re.compile(r'<skill name="[^"]*" path="([^"]+)">')


def skill_paths_in_messages(
    messages: list[AnyMessage],
    *,
    skills_root: str,
    read_tool_names: Collection[str],
) -> set[str]:
    """[argus patch #145] Skill files whose body is still in ``messages``.

    A successful read (the AI call plus its non-error ToolMessage) or an
    ``auto_skill_activation`` block that carries the body inline.
    """
    normalized_root = posixpath.normpath(skills_root.rstrip("/") or "/")
    read_names = frozenset(read_tool_names)
    paths_by_id: dict[str, str] = {}
    present: set[str] = set()
    for message in messages:
        if isinstance(message, AIMessage):
            for tool_call in message.tool_calls or []:
                if _tool_call_name(tool_call) not in read_names:
                    continue
                tool_call_id = _tool_call_id(tool_call)
                raw_path = _tool_call_path(tool_call)
                path = _normalize_under_root(raw_path, normalized_root) if raw_path else None
                if tool_call_id and path and _is_skill_file(path):
                    paths_by_id[tool_call_id] = path
        elif isinstance(message, ToolMessage):
            path = paths_by_id.get(str(message.tool_call_id or ""))
            content = message.content if isinstance(message.content, str) else ""
            if path and getattr(message, "status", "success") != "error" and not _is_tool_error_text(content):
                present.add(path)
        elif getattr(message, "name", None) == "auto_skill_activation" and isinstance(message.content, str):
            for raw in _ROUTED_SKILL_PATH_RE.findall(message.content):
                path = _normalize_under_root(unescape(raw), normalized_root)
                if path:
                    present.add(path)
    return present

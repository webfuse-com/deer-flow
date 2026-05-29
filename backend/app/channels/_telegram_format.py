"""[argus] Telegram HTML formatting + safe chunking.

DeerFlow's Telegram channel sends the agent's raw markdown with no parse_mode,
so Telegram renders it literally (asterisks, backticks, etc. shown as text).
This module converts agent markdown into Telegram-native HTML (parse_mode=HTML)
and splits long messages on the 4096-char limit without breaking tags.

Ported near-verbatim from the ateam Telegram bot (TOOLS/ateam-bot/formatting.py),
which was hardened over real use for code blocks, tables, escaping, and links.
Carried as Argus fork patch #10 — see PATCHES.md.
"""

from __future__ import annotations

import re

# Telegram's hard per-message ceiling.
TELEGRAM_MAX = 4096


def strip_agent_markers(text: str) -> str:
    """Remove stray LLM scaffolding markers some models leak into output."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"</?final>", "", text)
    text = text.replace("[[reply_to_current]]", "")
    return text.strip()


def _escape_html(text: str) -> str:
    """Escape the three HTML entities Telegram's HTML parser cares about."""
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    return text


def _markdown_table_to_box(table_text: str) -> str:
    """Convert a markdown pipe table into a box-drawn monospace block.

    Telegram has no table primitive; a box-drawn table inside <pre> renders
    legibly in a fixed-width font.
    """
    lines = [l.strip() for l in table_text.strip().splitlines() if l.strip()]

    rows = []
    separator_idx = -1
    for i, line in enumerate(lines):
        cells = [c.strip() for c in line.strip("|").split("|")]
        # Detect the markdown header separator row (---, :--:, ---:).
        if all(re.match(r"^[:\-]+$", c) for c in cells):
            separator_idx = i
            continue
        rows.append(cells)

    if not rows:
        return table_text

    n_cols = max(len(r) for r in rows)
    for r in rows:
        while len(r) < n_cols:
            r.append("")

    widths = []
    for col in range(n_cols):
        w = max(len(r[col]) for r in rows)
        widths.append(max(w, 1))

    def hline(left, mid, right, fill="─"):
        return left + mid.join(fill * (w + 2) for w in widths) + right

    def dataline(cells):
        parts = []
        for col, cell in enumerate(cells):
            parts.append(" " + cell.ljust(widths[col]) + " ")
        return "│" + "│".join(parts) + "│"

    result = [hline("┌", "┬", "┐")]
    for i, row in enumerate(rows):
        result.append(dataline(row))
        if i == 0 and (separator_idx != -1 or len(rows) > 1):
            result.append(hline("├", "┼", "┤"))
    result.append(hline("└", "┴", "┘"))

    return "\n".join(result)


def _convert_tables(text: str) -> str:
    """Replace markdown pipe tables with box-drawn blocks (stash-marked)."""
    table_pattern = re.compile(
        r"((?:^[ \t]*\|.+\|[ \t]*$\n?){2,})",
        re.MULTILINE,
    )

    def _replace(m):
        box = _markdown_table_to_box(m.group(1))
        return f"\x00TABLE_START\x00{box}\x00TABLE_END\x00"

    return table_pattern.sub(_replace, text)


def to_telegram_html(text: str) -> str:
    """Convert agent markdown output to Telegram-safe HTML.

    Strategy: stash code blocks / tables / inline code (raw, escaped) so their
    contents are never markdown-transformed, escape HTML entities in the rest,
    convert markdown patterns to HTML tags, then restore the stashed blocks.
    """
    text = strip_agent_markers(text)

    # Fenced code blocks → <pre><code>, stashed before escaping.
    code_blocks: list[str] = []

    def _stash_code_block(m):
        code = _escape_html(m.group(2).strip())
        code_blocks.append(f"<pre><code>{code}</code></pre>")
        return f"\x00CODEBLOCK{len(code_blocks) - 1}\x00"

    text = re.sub(r"```(\w*)\n?(.*?)```", _stash_code_block, text, flags=re.DOTALL)

    # Markdown tables → box-drawn <pre>, stashed.
    text = _convert_tables(text)
    tables: list[str] = []

    def _stash_table(m):
        table_content = _escape_html(m.group(1))
        tables.append(f"<pre>{table_content}</pre>")
        return f"\x00TABLE{len(tables) - 1}\x00"

    text = re.sub(r"\x00TABLE_START\x00(.*?)\x00TABLE_END\x00", _stash_table, text, flags=re.DOTALL)

    # Inline code → <code>, stashed.
    inline_codes: list[str] = []

    def _stash_inline(m):
        code = _escape_html(m.group(1))
        inline_codes.append(f"<code>{code}</code>")
        return f"\x00INLINE{len(inline_codes) - 1}\x00"

    text = re.sub(r"`([^`\n]+)`", _stash_inline, text)

    # Escape entities in everything that's left.
    text = _escape_html(text)

    # Links [text](url) → <a href="url">text</a>
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    # Headers ### Header → bold line
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    # Bold **text** → <b>
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    # Italic *text* → <i> (single asterisk, not part of a bold marker)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", text)
    # Strikethrough ~~text~~ → <s>
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    # Bullets - item / * item → • item
    text = re.sub(r"^[\-\*]\s+", "• ", text, flags=re.MULTILINE)

    # Consecutive > lines → expandable blockquote.
    def _collapse_blockquote(m):
        inner = re.sub(r"^&gt;\s?", "", m.group(0), flags=re.MULTILINE)
        return f"<blockquote expandable>{inner.strip()}</blockquote>"

    text = re.sub(r"(?:^&gt;[^\n]*\n?)+", _collapse_blockquote, text, flags=re.MULTILINE)

    # <details>…</details> (escaped) → expandable blockquote.
    text = re.sub(
        r"&lt;details&gt;\s*(.*?)\s*&lt;/details&gt;",
        r"<blockquote expandable>\1</blockquote>",
        text,
        flags=re.DOTALL,
    )

    # Restore stashed blocks.
    for i, block in enumerate(code_blocks):
        text = text.replace(f"\x00CODEBLOCK{i}\x00", block)
    for i, table in enumerate(tables):
        text = text.replace(f"\x00TABLE{i}\x00", table)
    for i, inline in enumerate(inline_codes):
        text = text.replace(f"\x00INLINE{i}\x00", inline)

    return text


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

# Tags we must not split across a chunk boundary (open in one, close in another
# would produce invalid HTML and a Telegram BadRequest). When a single chunk
# would exceed the limit mid-block we prefer to break at a safe boundary.
_TAG_RE = re.compile(r"<[^>]+>")


def chunk_html(html: str, limit: int = TELEGRAM_MAX) -> list[str]:
    """Split HTML into <= limit-sized chunks without cutting inside a tag.

    Prefers to break on paragraph (blank line) then newline boundaries; only
    falls back to a hard character cut if a single line exceeds the limit, and
    even then avoids cutting in the middle of a "<...>" tag. This is a
    best-effort splitter: the channel's send path also falls back to plain
    text if Telegram still rejects a chunk, so a pathological input degrades
    gracefully rather than dropping the message.
    """
    if len(html) <= limit:
        return [html] if html else []

    chunks: list[str] = []
    remaining = html

    while len(remaining) > limit:
        window = remaining[:limit]

        # Don't cut inside a tag: if the window ends inside "<...", back up to
        # just before the unclosed "<".
        last_open = window.rfind("<")
        last_close = window.rfind(">")
        if last_open > last_close:
            window = window[:last_open]

        # Prefer a paragraph break, then a newline, then a space.
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = len(window)  # hard cut (already tag-safe via the back-up above)

        chunk = remaining[:cut].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[cut:].lstrip()

    if remaining:
        chunks.append(remaining)
    return chunks

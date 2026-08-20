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
    lines = [line.strip() for line in table_text.strip().splitlines() if line.strip()]

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

# Block-level tags that must never be split across a chunk boundary — an open
# in one chunk and close in another is invalid HTML and Telegram rejects it.
# We tokenize the HTML into these blocks + plain runs, and pack whole blocks
# into chunks. A single block bigger than the limit is split into multiple
# self-closed blocks of the same kind.
_BLOCK_RE = re.compile(
    r"<pre>.*?</pre>"
    r"|<pre><code>.*?</code></pre>"
    r"|<blockquote(?:\s+expandable)?>.*?</blockquote>",
    re.DOTALL,
)


def _split_oversized_block(block: str, limit: int) -> list[str]:
    """Split a single block that's larger than `limit` into several valid
    blocks of the same kind. Used for huge <pre>/<code> dumps (e.g. an agent
    pasting a whole SVG). Splits on line boundaries inside the block."""
    # Order matters: try the <pre><code> pair before the bare <pre> so the
    # alternation doesn't match <pre> first and orphan the inner <code>.
    if block.startswith("<pre><code>") and block.endswith("</code></pre>"):
        open_tag, close_tag = "<pre><code>", "</code></pre>"
    elif block.startswith("<pre>") and block.endswith("</pre>"):
        open_tag, close_tag = "<pre>", "</pre>"
    elif block.startswith("<blockquote") and block.endswith("</blockquote>"):
        open_tag = block[: block.index(">") + 1]
        close_tag = "</blockquote>"
    else:
        # Not a recognized wrapper — fall back to a hard char split.
        return [block[i : i + limit] for i in range(0, len(block), limit)]
    inner = block[len(open_tag) : len(block) - len(close_tag)]
    budget = limit - len(open_tag) - len(close_tag)
    if budget <= 0:
        return [block[i : i + limit] for i in range(0, len(block), limit)]
    pieces: list[str] = []
    cur = ""
    for line in inner.splitlines(keepends=True):
        # A single line longer than the budget: hard-split the line.
        while len(line) > budget:
            if cur:
                pieces.append(open_tag + cur + close_tag)
                cur = ""
            pieces.append(open_tag + line[:budget] + close_tag)
            line = line[budget:]
        if len(cur) + len(line) > budget:
            pieces.append(open_tag + cur + close_tag)
            cur = line
        else:
            cur += line
    if cur:
        pieces.append(open_tag + cur + close_tag)
    return pieces


def chunk_html(html: str, limit: int = TELEGRAM_MAX) -> list[str]:
    """Split HTML into <= limit-sized chunks where every chunk is valid HTML.

    Strategy: tokenize into block elements (<pre>, <pre><code>, <blockquote>)
    and the plain runs between them, then greedily pack whole tokens into
    chunks. A block is never cut across a boundary; a block larger than the
    limit is itself split into several valid same-kind blocks. Plain runs that
    overflow are split on paragraph/newline/space boundaries (and never inside
    an inline tag). The channel's send path still falls back to plain text if
    Telegram somehow rejects a chunk, so the worst case degrades, never drops.
    """
    html = html or ""
    if len(html) <= limit:
        return [html] if html else []

    # 1. Tokenize: alternating plain runs and whole blocks.
    tokens: list[str] = []
    pos = 0
    for m in _BLOCK_RE.finditer(html):
        if m.start() > pos:
            tokens.append(html[pos : m.start()])
        tokens.append(m.group(0))
        pos = m.end()
    if pos < len(html):
        tokens.append(html[pos:])

    # 2. Expand any oversized token into limit-sized pieces.
    pieces: list[str] = []
    for tok in tokens:
        if len(tok) <= limit:
            pieces.append(tok)
        elif tok.startswith("<pre") or tok.startswith("<blockquote"):
            pieces.extend(_split_oversized_block(tok, limit))
        else:
            pieces.extend(_split_plain(tok, limit))

    # 3. Greedily pack pieces into chunks.
    chunks: list[str] = []
    cur = ""
    for p in pieces:
        if not cur:
            cur = p
        elif len(cur) + 1 + len(p) <= limit:
            cur = cur + ("" if cur.endswith("\n") or p.startswith("\n") else "\n") + p
        else:
            chunks.append(cur.strip("\n"))
            cur = p
    if cur.strip():
        chunks.append(cur.strip("\n"))
    return [c for c in chunks if c]


def _split_plain(text: str, limit: int) -> list[str]:
    """Split a plain (non-block) run on paragraph/newline/space boundaries,
    never inside an inline "<...>" tag."""
    out: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        last_open = window.rfind("<")
        last_close = window.rfind(">")
        if last_open > last_close:
            window = window[:last_open]
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = len(window)
        seg = remaining[:cut]
        if seg.strip():
            out.append(seg)
        remaining = remaining[cut:].lstrip("\n")
    if remaining.strip():
        out.append(remaining)
    return out

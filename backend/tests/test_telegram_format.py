"""[argus] Tests for the Telegram HTML formatter + chunker (fork patch #10)."""

from __future__ import annotations

from app.channels._telegram_format import chunk_html, to_telegram_html


def test_bold_italic_strike():
    assert to_telegram_html("**b**") == "<b>b</b>"
    assert to_telegram_html("*i*") == "<i>i</i>"
    assert to_telegram_html("~~s~~") == "<s>s</s>"


def test_header_becomes_bold():
    assert to_telegram_html("### Title") == "<b>Title</b>"


def test_link():
    assert to_telegram_html("[click](https://x.io)") == '<a href="https://x.io">click</a>'


def test_inline_code_is_escaped_and_wrapped():
    out = to_telegram_html("use `a < b` here")
    assert "<code>a &lt; b</code>" in out
    # The surrounding text's literal angle is escaped too.
    assert "use " in out


def test_fenced_code_block_preserved_and_escaped():
    md = "```python\nif a < b:\n    pass\n```"
    out = to_telegram_html(md)
    assert out.startswith("<pre><code>")
    assert "if a &lt; b:" in out
    # Markdown inside a code block must NOT be transformed.
    assert "<b>" not in out


def test_bare_html_in_prose_is_escaped():
    # A naked < in prose must not become a broken tag.
    out = to_telegram_html("5 < 10 and b > a")
    assert "&lt;" in out and "&gt;" in out
    assert "<10" not in out


def test_bullets():
    out = to_telegram_html("- one\n- two")
    assert "• one" in out and "• two" in out


def test_table_to_box():
    md = "| A | B |\n|---|---|\n| 1 | 2 |"
    out = to_telegram_html(md)
    assert "<pre>" in out and "│" in out and "┌" in out


def test_blockquote_collapses():
    out = to_telegram_html("> line one\n> line two")
    assert "<blockquote expandable>" in out


def test_chunk_short_passthrough():
    assert chunk_html("hello") == ["hello"]
    assert chunk_html("") == []


def test_chunk_respects_limit():
    text = "\n".join(f"line {i} " + "x" * 40 for i in range(500))
    chunks = chunk_html(text, limit=4096)
    assert len(chunks) > 1
    assert all(len(c) <= 4096 for c in chunks)
    # No content silently dropped (modulo whitespace at the join seams).
    assert sum(c.count("line ") for c in chunks) == 500


def test_chunk_does_not_split_inside_tag():
    # Build text where the naive cut would land inside an <a href> tag.
    prefix = "x" * 4090
    text = prefix + '<a href="https://example.com/long/path">link</a>'
    chunks = chunk_html(text, limit=4096)
    for c in chunks:
        # An opening "<a" in a chunk must have its closing ">" in the same chunk.
        assert c.count("<a ") == c.count("</a>") or "<a " not in c or ">" in c[c.rfind("<a "):]

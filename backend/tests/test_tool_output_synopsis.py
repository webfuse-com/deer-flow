"""Tests for ToolOutputSynopsis and code outline on large reads (Patch #85)."""

from __future__ import annotations

import re

from deerflow.agents.middlewares.tool_output_budget_middleware import (
    ToolOutputBudgetMiddleware,
    _budget_content,
)
from deerflow.agents.middlewares.tool_output_synopsis import (
    OUTLINE_SYMBOL_LIMIT,
    build_tool_output_synopsis,
    render_tool_output_preview,
)
from deerflow.config.tool_output_config import ToolOutputConfig


def _generate_js_incident_fixture() -> str:
    """Generate a JS fixture (>300 lines) matching the incident shape.

    Contains function renderOverlays() with inline toolbar rendering,
    and NO top-level renderToolbar symbol.
    """
    lines = [
        "// webfuse-annotate content script",
        "import { setupUI } from './ui';",
        "const VERSION = '1.0.0';",
        "",
        "function initExtension() {",
        "    console.log('init');",
        "}",
        "",
    ]
    # Pad before renderOverlays with a few symbols (less than 40 so renderOverlays isn't capped away)
    for i in range(10):
        lines.append(f"const helper_{i} = () => {{ return {i}; }};")

    lines.append("function renderOverlays() {")
    lines.append("    // Inside renderOverlays, toolbar is created inline:")
    lines.append("    const renderToolbar = document.createElement('div');")
    lines.append("    renderToolbar.className = 'annotate-toolbar';")
    for i in range(250):
        lines.append(f"    renderToolbar.setAttribute('data-item-{i}', '{i}');")
    lines.append("    document.body.appendChild(renderToolbar);")
    lines.append("}")
    lines.append("")

    # Pad after renderOverlays with another symbol
    lines.append("function cleanup() {")
    lines.append("    console.log('cleanup');")
    lines.append("}")
    for i in range(60):
        lines.append(f"// padding line {i}")

    return "\n".join(lines)


def test_case_1_incident_regression():
    """1. Incident regression: JS fixture > 300 lines with renderOverlays and inline toolbar.

    With code_outline_enabled=True and content >= 300 lines:
    - notable_items contains an entry like 'renderOverlays [lines a-b]'
    - does NOT contain a separate 'renderToolbar'
    - at least one entry matches r'\\[lines \\d+-\\d+\\]'
    """
    content = _generate_js_incident_fixture()
    assert len(content.splitlines()) > 300

    synopsis = build_tool_output_synopsis(
        content,
        code_outline_enabled=True,
        code_outline_min_lines=300,
    )
    assert synopsis.is_outline is True
    assert any("renderOverlays" in item and re.search(r"\[lines \d+-\d+\]", item) for item in synopsis.notable_items)
    assert not any("renderToolbar" in item for item in synopsis.notable_items)
    assert any(re.search(r"\[lines \d+-\d+\]", item) for item in synopsis.notable_items)


def test_case_2_gate_on_above_min_lines():
    """2. Gate ON + code > min_lines -> outline has line numbers."""
    code_lines = [
        "import sys",
        "",
        "def first_func():",
        "    pass",
        "",
    ]
    for i in range(350):
        code_lines.append(f"    # line {i}")
    code_lines.extend(
        [
            "",
            "def second_func():",
            "    pass",
        ]
    )
    for i in range(50):
        code_lines.append(f"    # trailing line {i}")

    content = "\n".join(code_lines)
    assert len(content.splitlines()) >= 300

    synopsis = build_tool_output_synopsis(
        content,
        code_outline_enabled=True,
        code_outline_min_lines=300,
    )
    assert synopsis.is_outline is True
    assert len(synopsis.notable_items) >= 2
    assert any(re.search(r"\[lines \d+-\d+\]", item) for item in synopsis.notable_items)
    assert any("first_func" in item for item in synopsis.notable_items)
    assert any("second_func" in item for item in synopsis.notable_items)


def test_case_3_gate_on_below_min_lines():
    """3. Gate ON + code < min_lines -> old symbol-list behavior, no line numbers."""
    content = "import os\n\ndef my_small_func():\n    return 42\n"
    synopsis = build_tool_output_synopsis(
        content,
        code_outline_enabled=True,
        code_outline_min_lines=300,
    )
    assert synopsis.is_outline is False
    assert synopsis.notable_items == ["def my_small_func"]


def test_case_4_gate_off_byte_identical():
    """4. Gate OFF -> byte-identical old behavior (assert a known legacy output)."""
    code_lines = ["import math", ""]
    for i in range(400):
        code_lines.append(f"def func_{i}():\n    return {i}")
    content = "\n".join(code_lines)

    # Legacy call with default args
    synopsis_default = build_tool_output_synopsis(content)
    assert synopsis_default.is_outline is False
    # Check that notable_items has the old format (kind name, no line numbers)
    assert synopsis_default.notable_items[0] == "def func_0"
    assert not any("line" in item for item in synopsis_default.notable_items)

    # Explicit gate OFF
    synopsis_off = build_tool_output_synopsis(
        content,
        code_outline_enabled=False,
        code_outline_min_lines=300,
    )
    assert synopsis_off == synopsis_default


def test_case_5_symbol_cap():
    """5. Symbol cap: >40 symbols -> exactly 40 entries + a '+N more' marker."""
    code_lines = ["import os", ""]
    for i in range(50):
        code_lines.append(f"def func_{i:02d}():")
        for j in range(8):
            code_lines.append(f"    # body {j}")
    content = "\n".join(code_lines)
    assert len(content.splitlines()) > 300

    synopsis = build_tool_output_synopsis(
        content,
        code_outline_enabled=True,
        code_outline_min_lines=300,
    )
    assert synopsis.is_outline is True
    # Exactly OUTLINE_SYMBOL_LIMIT (40) + 1 marker = 41 items
    assert len(synopsis.notable_items) == OUTLINE_SYMBOL_LIMIT + 1
    assert synopsis.notable_items[-1] == "... (+10 more)"


def test_case_6_line_numbers_are_accurate():
    """6. Line numbers are accurate: a symbol at a known line in the fixture reports that line."""
    code_lines = ["import sys"]  # line 1
    # pad up to line 100
    while len(code_lines) < 99:
        code_lines.append(f"// line {len(code_lines) + 1}")
    code_lines.append("def target_function():")  # line 100 (1-indexed)
    code_lines.append("    pass")  # line 101
    while len(code_lines) < 199:
        code_lines.append(f"// line {len(code_lines) + 1}")
    code_lines.append("def next_function():")  # line 200
    code_lines.append("    pass")
    while len(code_lines) < 320:
        code_lines.append(f"// line {len(code_lines) + 1}")

    content = "\n".join(code_lines)
    lines = content.splitlines()
    assert lines[99] == "def target_function():"
    assert lines[199] == "def next_function():"

    synopsis = build_tool_output_synopsis(
        content,
        code_outline_enabled=True,
        code_outline_min_lines=300,
    )
    assert synopsis.is_outline is True
    assert "target_function [lines 100-199]" in synopsis.notable_items
    assert "next_function [line 200]" in synopsis.notable_items or "next_function [lines 200-" in str(synopsis.notable_items)


def test_case_7_arrow_const_exports():
    """7. Arrow/const/exports: const foo = () =>, export const bar, export function baz appear with line numbers."""
    code_lines = [
        "import { helper } from './utils';",
        "const foo = () => {",
        "    return 1;",
        "};",
        "export const bar = 42;",
        "export function baz() {",
        "    return 'baz';",
        "}",
        "class MyClass {",
        "}",
        "async function asyncWork() {",
        "}",
    ]
    # Pad to > 300 lines
    for i in range(310):
        code_lines.append(f"// pad {i}")

    content = "\n".join(code_lines)
    synopsis = build_tool_output_synopsis(
        content,
        code_outline_enabled=True,
        code_outline_min_lines=300,
    )
    assert synopsis.is_outline is True
    items_str = "\n".join(synopsis.notable_items)
    assert "foo" in items_str
    assert "bar" in items_str
    assert "baz" in items_str
    assert "MyClass" in items_str
    assert "asyncWork" in items_str
    # All captured symbols have line numbers
    for item in synopsis.notable_items:
        assert re.search(r"\[lines? \d+(-\d+)?\]", item)


def test_case_8_non_code_prose_gate_on():
    """8. Non-code (prose) with gate ON -> no outline, unchanged."""
    prose_lines = ["# Title of Document", "", "This is a long prose document without code."]
    for i in range(400):
        prose_lines.append(f"Paragraph {i}: The quick brown fox jumps over the lazy dog.")
    content = "\n".join(prose_lines)

    synopsis = build_tool_output_synopsis(
        content,
        code_outline_enabled=True,
        code_outline_min_lines=300,
    )
    assert synopsis.kind == "text"
    assert synopsis.is_outline is False
    assert synopsis.notable_items == []


def test_case_9_renderer_steering_line():
    """9. Renderer appends the steering line only on the outline path."""
    code_lines = ["import math", ""]
    for i in range(350):
        code_lines.append(f"def func_{i}():\n    pass")
    large_code = "\n".join(code_lines)

    small_code = "import math\ndef func():\n    pass\n"

    steering_line = "- Outline above: use read_file with start_line and end_line on the referenced ranges instead of re-reading the whole file."

    # Render with outline active:
    rendered_outline = render_tool_output_preview(
        large_code,
        tool_name="bash",
        virtual_path="/mnt/user-data/outputs/.tool-results/bash-123.log",
        head_chars=100,
        tail_chars=100,
        code_outline_enabled=True,
        code_outline_min_lines=300,
    )
    assert steering_line in rendered_outline

    # Render with small code: steering line must NOT appear
    rendered_small = render_tool_output_preview(
        small_code,
        tool_name="bash",
        virtual_path="/mnt/user-data/outputs/.tool-results/bash-123.log",
        head_chars=100,
        tail_chars=100,
        code_outline_enabled=True,
        code_outline_min_lines=300,
    )
    assert steering_line not in rendered_small

    # Render with gate OFF: steering line must NOT appear
    rendered_off = render_tool_output_preview(
        large_code,
        tool_name="bash",
        virtual_path="/mnt/user-data/outputs/.tool-results/bash-123.log",
        head_chars=100,
        tail_chars=100,
        code_outline_enabled=False,
        code_outline_min_lines=300,
    )
    assert steering_line not in rendered_off


def test_outline_no_symbols_no_steering_line():
    """Corner case: code > min_lines with zero captured top-level symbols.

    When gate is on, looks_code matches, and lines > min_lines, but no top-level
    symbols are found, is_outline must remain False and no steering line is appended.
    """
    code_lines = ["import math", ""]
    # Indented lines only, so no top-level symbols match
    for i in range(350):
        code_lines.append(f"    x_{i} = {i}")
    content = "\n".join(code_lines)

    synopsis = build_tool_output_synopsis(
        content,
        code_outline_enabled=True,
        code_outline_min_lines=300,
    )
    assert synopsis.kind == "code"
    assert synopsis.is_outline is False

    steering_line = "- Outline above: use read_file with start_line and end_line on the referenced ranges instead of re-reading the whole file."
    rendered = render_tool_output_preview(
        content,
        tool_name="bash",
        virtual_path="/mnt/user-data/outputs/.tool-results/bash-123.log",
        head_chars=100,
        tail_chars=100,
        code_outline_enabled=True,
        code_outline_min_lines=300,
    )
    assert steering_line not in rendered


def test_case_10_middleware_wiring(tmp_path):
    """10. Middleware wiring: ToolOutputBudgetMiddleware.from_config with code_outline_enabled=True

    produces the outline on an oversized bash code result; with default config it does not
    (assert via the public _budget_content / render_tool_output_preview path used by the middleware).
    """
    code_lines = ["import os", ""]
    for i in range(400):
        code_lines.append(f"def func_{i}():\n    # do something {i}\n    pass")
    content = "\n".join(code_lines)
    # Ensure content is large enough to trigger externalization (> 12000 chars)
    assert len(content) > 12000

    config_enabled = ToolOutputConfig(code_outline_enabled=True, code_outline_min_lines=300)
    config_default = ToolOutputConfig()

    # Test ToolOutputBudgetMiddleware.from_config wiring
    mw_enabled = ToolOutputBudgetMiddleware.from_config(config_enabled)
    assert mw_enabled._config.code_outline_enabled is True
    assert mw_enabled._config.code_outline_min_lines == 300

    mw_default = ToolOutputBudgetMiddleware.from_config(config_default)
    assert mw_default._config.code_outline_enabled is False

    steering_line = "- Outline above: use read_file with start_line and end_line on the referenced ranges instead of re-reading the whole file."

    # With gate enabled:
    budgeted_enabled = _budget_content(
        content,
        tool_name="bash",
        tool_call_id="tc-1",
        outputs_path=str(tmp_path),
        config=mw_enabled._config,
    )
    assert budgeted_enabled is not None
    assert steering_line in budgeted_enabled
    assert "[lines " in budgeted_enabled

    # With default config:
    budgeted_default = _budget_content(
        content,
        tool_name="bash",
        tool_call_id="tc-2",
        outputs_path=str(tmp_path),
        config=mw_default._config,
    )
    assert budgeted_default is not None
    assert steering_line not in budgeted_default
    assert "[lines " not in budgeted_default

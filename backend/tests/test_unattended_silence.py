"""[argus patch #32] Trivial-filler suppression for unattended turns.

Patch #31 made a fully-empty unattended (scheduled-playbook) turn stay silent.
But a model told to "produce no output" frequently emits a single filler token
instead of nothing -- a lone "." being the common case for the hourly
meeting-prep poll. This locks `_is_trivial_unattended_text`, the predicate the
two delivery guards in `manager.py` use to collapse such filler to empty so the
existing unattended-silence branch fires.

The contract: blank out only genuinely contentless filler; never drop a real
brief. Interactive turns do not go through this predicate at all.
"""

from __future__ import annotations

import pytest

from app.channels.manager import _is_trivial_unattended_text


class TestIsTrivialUnattendedText:
    @pytest.mark.parametrize(
        "text",
        [
            "",  # fully empty
            "   ",  # whitespace only
            "\n\t ",  # mixed whitespace
            None,  # no response object at all
            ".",  # the reported meeting-prep filler
            "..",
            "...",
            " . ",  # filler with surrounding whitespace
            "-",
            "--",
            "…",  # a single ellipsis character
        ],
    )
    def test_trivial_filler_is_blanked(self, text):
        assert _is_trivial_unattended_text(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "ok",  # short but real word
            "No.",  # has a word char
            "1",  # a digit is real content
            "a",  # single alnum char
            "Meeting with Acme at 15:00",  # a genuine brief
            "....",  # 4+ chars: out of the trivial window
            "- Talking point one",  # bullet brief, not filler
            "Who: Jane (Acme)\nContext: ...",  # multiline brief
        ],
    )
    def test_real_content_is_preserved(self, text):
        assert _is_trivial_unattended_text(text) is False

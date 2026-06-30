"""[argus patch #32/#34] Trivial-filler + silence-announcement suppression for
unattended turns.

Patch #31 made a fully-empty unattended (scheduled-playbook) turn stay silent.
Patch #32: a model told to "produce no output" frequently emits a single filler
token instead of nothing -- a lone "." being the common case for the hourly
meeting-prep poll. Patch #34: a model that instead NARRATES its silence in a
full sentence ("No meetings in the window. Staying silent.") -- itself the noise
the silence branch exists to drop. This locks `_is_trivial_unattended_text`, the
predicate the two delivery guards in `manager.py` use to collapse such output to
empty so the existing unattended-silence branch fires.

The contract: blank out only genuinely contentless filler or a short, anchored
"nothing to report / staying silent" announcement; never drop a real brief.
Interactive turns do not go through this predicate at all.
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
            "No meetings in the window. Staying silent.",  # the reported message
            "no meetings in the window. staying silent.",  # lowercase
            "No meetings in the next 15-20 minutes. Staying silent.",
            "No upcoming meetings.",
            "No meetings to report.",
            "Nothing to report.",
            "Nothing scheduled. Staying silent.",
            "No events in the window.",
            "Staying silent.",
            "I'll stay silent.",
            "No output.",
            "- No meetings in the window. Staying silent.",  # leading bullet
        ],
    )
    def test_silence_announcement_is_blanked(self, text):
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
            # A genuine brief that happens to mention a meeting / no-show /
            # the word "silent" must NOT be suppressed:
            "Meeting at 15:00 with Acme. They went silent last week, so push for a timeline.",
            "Standup in 15 min. No blockers reported by the team; ship the release.",
            "Who: Bob (Acme), Jane (Globex)\nContext: prior thread on pricing\nTalking points:\n- renewal\n- no upcoming gaps",
            # Long enough to exceed the announcement cap even if phrased like one:
            "No meetings in the window, but here is the daily digest you asked for: "
            "three PRs merged, two issues opened, and the Acme renewal call is confirmed for tomorrow at 10.",
        ],
    )
    def test_real_content_is_preserved(self, text):
        assert _is_trivial_unattended_text(text) is False

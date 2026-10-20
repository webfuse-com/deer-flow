"""[argus patch #151] A scheduled run that ends on a closing note still delivers its content.

atlas-dima's morning brief told the model to append the day's topic keys to a
log "after delivering". The model wrote the brief in the AI message that also
carried the write_file call, then ended on a closing note, and delivery sends
only the run's last AI message: Telegram got "Brief delivered and topic log
updated." and the brief was lost (Chronos 42764 on 10-13, 43020 on 10-14,
46257 on 10-20). The fixtures below are those three runs' current-turn
messages (deerflow_atlas_dima.run_events) with the AI texts verbatim, plus
two atlas-ashish scheduled runs whose short final message IS the report.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from _router_auth_helpers import make_authed_test_app
from langgraph_sdk.client import LangGraphClient

from app.channels._final_reply import _recover_closing_note_content
from app.channels.manager import ChannelManager
from app.channels.message_bus import InboundMessage, MessageBus
from app.channels.store import JsonChannelStore
from app.gateway.routers import thread_runs
from deerflow.runtime import DisconnectMode, RunRecord, RunStatus
from deerflow.runtime.stream_bridge.memory import MemoryStreamBridge

REPORT_URL = "http://argus-scheduler:8000/api/runs/46257/output"


RUN_fe70a0cc = [
    {"type": "human", "content": "Good morning. Produce today's briefing for Telegram."},
    {
        "type": "ai",
        "content": "\n\n",
        "tool_calls": [
            {"name": "read_file", "args": {}, "id": "call_18bb242d191b48d9bf3a9f0c", "type": "tool_call"},
            {"name": "calendar_list_events", "args": {}, "id": "call_2570384631d84710a7d95b70", "type": "tool_call"},
            {"name": "gmail_list_threads", "args": {}, "id": "call_d3c13c33c1c04b2e99b843c4", "type": "tool_call"},
        ],
    },
    {"type": "tool", "name": "gmail_list_threads", "content": '[{"type": "text", "text": "No threads matched query: in:inbo'},
    {"type": "tool", "name": "calendar_list_events", "content": '[{"type": "text", "text": "{\\n  \\"current_time\\": \\"2026-10-'},
    {"type": "tool", "name": "read_file", "content": "# Briefing topic log\n\nNewest section last. Used by Atlas to "},
    {"type": "ai", "content": "\n\n", "tool_calls": [{"name": "pythia_query", "args": {}, "id": "call_efd3074cc35d4a5fb36fffcb", "type": "tool_call"}]},
    {"type": "tool", "name": "pythia_query", "content": '[{"type": "text", "text": "[chunk 78480 | doc 35133] DevOps '},
    {
        "type": "ai",
        "content": "\n"
        "\n"
        "Morning brief — Tuesday, 13 October 2026\n"
        "Inbox:\n"
        "- clear\n"
        "Calendar:\n"
        "- standard\n"
        "Tasks:\n"
        "- on track\n"
        "Watch:\n"
        "- Collaborate client certificate expires ~17 Oct; renewal script fails on geo-restricted DNS. Tony pinning manually — confirm it landed.\n"
        "- Yesterday's triple node-test failure (vrec_node_test 639978, vrec_regions_test 639975, regions_test 639976) still unowned.\n"
        "- Dashboard primary node split: two-week notice started 9 Oct, switchover date not set.\n"
        "\n",
        "tool_calls": [{"name": "write_file", "args": {}, "id": "call_243db690cdbb4b4daa6283b9", "type": "tool_call"}],
    },
    {"type": "tool", "name": "write_file", "content": "OK"},
    {"type": "ai", "content": "\n\nBrief above; log updated.", "tool_calls": []},
]


RUN_ddf9c4e4 = [
    {"type": "human", "content": "Good morning. Produce today's briefing for Telegram."},
    {
        "type": "ai",
        "content": "\n\n",
        "tool_calls": [
            {"name": "read_file", "args": {}, "id": "call_0fd94f5f909e4841be344dac", "type": "tool_call"},
            {"name": "gmail_list_threads", "args": {}, "id": "call_39186676e047468fb1836d5e", "type": "tool_call"},
            {"name": "calendar_list_events", "args": {}, "id": "call_0880c14519574991b33d8b30", "type": "tool_call"},
            {"name": "asana_my_tasks", "args": {}, "id": "call_f71563132ccb460c9da439e3", "type": "tool_call"},
            {"name": "pythia_query", "args": {}, "id": "call_04e2b8886ddd4252ab12c571", "type": "tool_call"},
        ],
    },
    {"type": "tool", "name": "calendar_list_events", "content": '[{"type": "text", "text": "{\\n  \\"current_time\\": \\"2026-10-'},
    {"type": "tool", "name": "pythia_query", "content": '[{"type": "text", "text": "No Pythia hits for \\"incident ale'},
    {"type": "tool", "name": "gmail_list_threads", "content": '[{"type": "text", "text": "[\\n  {\\n    \\"thread_id\\": \\"1a12'},
    {"type": "tool", "name": "asana_my_tasks", "content": '[{"type": "text", "text": "[\\n  {\\n    \\"gid\\": \\"1210015827'},
    {"type": "tool", "name": "read_file", "content": "# Briefing topic log\n\nNewest section last. Used by Atlas to "},
    {
        "type": "ai",
        "content": "\n\n",
        "tool_calls": [{"name": "calendar_list_events", "args": {}, "id": "call_843c21445aae49fb90a7c8b1", "type": "tool_call"}, {"name": "grep", "args": {}, "id": "call_26ba31bb109b4926b7e03c5b", "type": "tool_call"}],
    },
    {"type": "tool", "name": "calendar_list_events", "content": '[{"type": "text", "text": "{\\n  \\"current_time\\": \\"2026-10-'},
    {"type": "tool", "name": "grep", "content": "No matches found under /mnt/user-data/outputs/.tool-results/"},
    {
        "type": "ai",
        "content": "\n"
        "\n"
        "*Morning brief — Wed 14 Oct*\n"
        "\n"
        "*Inbox*\n"
        "• clear\n"
        "\n"
        "*Calendar*\n"
        "• Devops meeting at 10:25 — standup with Yauhen, Sasha, Tony; bring on-call status\n"
        "• [Webvirt daily](https://webfu.se/+webvirt/) at 10:40 — Pablo hasn't responded; cobro#12638 and it#8435/8468 updates\n"
        "\n"
        "*Tasks*\n"
        "• on track\n"
        "\n"
        "*Watch*\n"
        "• quiet\n"
        "\n",
        "tool_calls": [{"name": "write_file", "args": {}, "id": "call_c09cd875f592466680a11b18", "type": "tool_call"}],
    },
    {"type": "tool", "name": "write_file", "content": "OK"},
    {"type": "ai", "content": "\n\nBrief is delivered above — nothing further pending.", "tool_calls": []},
]


RUN_7a42df3e = [
    {"type": "human", "content": "Good morning. Produce today's briefing for Telegram."},
    {
        "type": "ai",
        "content": "\n\n",
        "tool_calls": [
            {"name": "read_file", "args": {}, "id": "call_11dc25144bb34fdcbb00cd91", "type": "tool_call"},
            {"name": "gmail_list_threads", "args": {}, "id": "call_2c28b2b66c0041b29e69c6b3", "type": "tool_call"},
            {"name": "calendar_list_events", "args": {}, "id": "call_a9d4f72751a34f62894ce3b8", "type": "tool_call"},
            {"name": "asana_my_tasks", "args": {}, "id": "call_55bc545580394886a95832d7", "type": "tool_call"},
        ],
    },
    {"type": "tool", "name": "read_file", "content": "# Briefing topic log\n\nNewest section last. Used by Atlas to "},
    {"type": "tool", "name": "calendar_list_events", "content": '[{"type": "text", "text": "{\\n  \\"current_time\\": \\"2026-10-'},
    {"type": "tool", "name": "asana_my_tasks", "content": '[{"type": "text", "text": "[\\n  {\\n    \\"gid\\": \\"1210015827'},
    {"type": "tool", "name": "gmail_list_threads", "content": '[{"type": "text", "text": "[\\n  {\\n    \\"thread_id\\": \\"1a15'},
    {"type": "ai", "content": "\n\n", "tool_calls": [{"name": "pythia_query", "args": {}, "id": "call_d087a322a3604481b8669853", "type": "tool_call"}]},
    {"type": "tool", "name": "pythia_query", "content": '[{"type": "text", "text": "[chunk 79114 | doc 35285] Support'},
    {"type": "ai", "content": "\n\n", "tool_calls": [{"name": "grep", "args": {}, "id": "call_9afa87bb471141289e3a4cd7", "type": "tool_call"}]},
    {"type": "tool", "name": "grep", "content": "Found 4 matches under /mnt/user-data/outputs/.tool-results/p"},
    {
        "type": "ai",
        "content": "\n"
        "\n"
        "*Morning brief — Tue 20 Oct*\n"
        "\n"
        "*Inbox*\n"
        "• Drata security compliance — actions assigned to you, due today\n"
        "• Checkly us-west-1 IP change notice — allowlist update before Nov 16\n"
        "\n"
        "*Calendar*\n"
        "• Devops meeting at 11:00 — network recording status\n"
        "• Webvirt meeting at 11:15 — you organize, Yuri hasn't RSVP'd\n"
        "\n"
        "*Tasks*\n"
        "• on track\n"
        "\n"
        "*Watch*\n"
        "• [vrec_node_test failed, build 644483](https://mrt-wake.surfly.com/build/644483) — vrec-stag nodes, unacked\n"
        "\n",
        "tool_calls": [{"name": "write_file", "args": {}, "id": "call_98bea38bf73b4712a603936e", "type": "tool_call"}],
    },
    {"type": "tool", "name": "write_file", "content": "OK"},
    {"type": "ai", "content": "\n\nBrief delivered and topic log updated.", "tool_calls": []},
]


RUN_5a327e24 = [
    {"type": "human", "content": "Refresh the FDE Trial Status dashboard. Follow /mnt/projects/fde-trial-status/REFRESH.md exactly (re"},
    {"type": "ai", "content": "\n\n", "tool_calls": [{"name": "read_file", "args": {}, "id": "call_a6cf87d52eff4b6f884ee627", "type": "tool_call"}]},
    {"type": "tool", "name": "read_file", "content": "# FDE Trial Status — refresh procedure\n\nApp: /mnt/apps/fde-t"},
    {
        "type": "ai",
        "content": "\n\n",
        "tool_calls": [
            {"name": "app_function_call", "args": {}, "id": "call_3d54cf6685484b249b411da2", "type": "tool_call"},
            {"name": "read_file", "args": {}, "id": "call_f318c8e702dc4094b7ac6e60", "type": "tool_call"},
            {"name": "read_file", "args": {}, "id": "call_2a4cb6c8ff1b46c283ecfe74", "type": "tool_call"},
        ],
    },
    {"type": "tool", "name": "read_file", "content": "[]\n"},
    {"type": "tool", "name": "read_file", "content": '{\n "refreshed_at": "2026-10-17T15:29:00+02:00",\n "refreshed_'},
    {"type": "tool", "name": "app_function_call", "content": '{"function": "get_requests", "risk": "read", "result": {"ite'},
    {"type": "ai", "content": "\n\n", "tool_calls": [{"name": "bash", "args": {}, "id": "call_5a5ef27d7e3f48378f6504f5", "type": "tool_call"}]},
    {"type": "tool", "name": "bash", "content": '[\n {\n  "name": "Brandon Whitman",\n  "company": "NICE SE",\n  '},
    {"type": "ai", "content": "\n\n", "tool_calls": [{"name": "bq_query", "args": {}, "id": "call_6145877f18114bfea1e52764", "type": "tool_call"}]},
    {"type": "tool", "name": "bq_query", "content": '[{"type": "text", "text": "{\\"rows\\": [{\\"email\\": \\"durgasa'},
    {
        "type": "ai",
        "content": "\n\n",
        "tool_calls": [
            {"name": "gmail_list_threads", "args": {}, "id": "call_dfbb728ef4af4ee0ae35edb5", "type": "tool_call"},
            {"name": "gmail_list_threads", "args": {}, "id": "call_1b504c2d27774f489799a897", "type": "tool_call"},
            {"name": "gmail_list_threads", "args": {}, "id": "call_547153e858d8410f9504342f", "type": "tool_call"},
            {"name": "gmail_list_threads", "args": {}, "id": "call_ae9cba961474474aaa9f97ec", "type": "tool_call"},
            {"name": "gmail_list_threads", "args": {}, "id": "call_26281e42b0a047b28913bc5d", "type": "tool_call"},
            {"name": "gmail_list_threads", "args": {}, "id": "call_e5cec9492d4144f18758aeca", "type": "tool_call"},
            {"name": "gmail_list_threads", "args": {}, "id": "call_45c06b49122a479a9c907ab6", "type": "tool_call"},
        ],
    },
    {"type": "tool", "name": "gmail_list_threads", "content": '[{"type": "text", "text": "[\\n  {\\n    \\"thread_id\\": \\"1a13'},
    {"type": "tool", "name": "gmail_list_threads", "content": '[{"type": "text", "text": "[\\n  {\\n    \\"thread_id\\": \\"1a13'},
    {"type": "tool", "name": "gmail_list_threads", "content": '[{"type": "text", "text": "[\\n  {\\n    \\"thread_id\\": \\"1a13'},
    {"type": "tool", "name": "gmail_list_threads", "content": '[{"type": "text", "text": "[\\n  {\\n    \\"thread_id\\": \\"1a13'},
    {"type": "tool", "name": "gmail_list_threads", "content": '[{"type": "text", "text": "[\\n  {\\n    \\"thread_id\\": \\"1a13'},
    {"type": "tool", "name": "gmail_list_threads", "content": '[{"type": "text", "text": "[\\n  {\\n    \\"thread_id\\": \\"1a12'},
    {"type": "tool", "name": "gmail_list_threads", "content": '[{"type": "text", "text": "[\\n  {\\n    \\"thread_id\\": \\"1a12'},
    {
        "type": "ai",
        "content": "\n"
        "\n"
        "All sources match the previous run exactly: same 7-person cohort, same user-existence hits (Durgasai, Michelle, Paul), and every Sent-folder date identical, so no new signups or touch "
        "points. Only the refresh stamp needs updating.\n"
        "\n",
        "tool_calls": [{"name": "bash", "args": {}, "id": "call_4e8c7b41f4c946f280473963", "type": "tool_call"}],
    },
    {"type": "tool", "name": "bash", "content": "2026-10-17T17:25:19+02:00\n2026-10-17T17:25:19+0200"},
    {"type": "ai", "content": "\n\n", "tool_calls": [{"name": "bash", "args": {}, "id": "call_b3bec5cacee84b2291fff32b", "type": "tool_call"}]},
    {"type": "tool", "name": "bash", "content": "ok, rows: 7"},
    {"type": "ai", "content": "\n\n", "tool_calls": [{"name": "atlas_commit_app", "args": {}, "id": "call_25ce01208e79425b9f9ff33c", "type": "tool_call"}]},
    {"type": "tool", "name": "atlas_commit_app", "content": '[{"type": "text", "text": "{\\n  \\"committed\\": true,\\n  \\"co'},
    {"type": "ai", "content": "\n\nFDE trials: 7 tracked / 4 no user yet / 3 handed to Intercom.\nNo change since last run (same cohort, same signups, same touch points).\nRefresh stamp updated, committed 080767a.", "tool_calls": []},
]


RUN_78babcd7 = [
    {"type": "human", "content": "Refresh the FDE Trial Status dashboard. Follow /mnt/projects/fde-trial-status/REFRESH.md exactly (re"},
    {"type": "ai", "content": "\n\n", "tool_calls": [{"name": "read_file", "args": {}, "id": "call_2f0c6198861045dabebb5f06", "type": "tool_call"}]},
    {"type": "tool", "name": "read_file", "content": "# FDE Trial Status — refresh procedure\n\nApp: /mnt/apps/fde-t"},
    {
        "type": "ai",
        "content": "\n\n",
        "tool_calls": [{"name": "read_file", "args": {}, "id": "call_4347eb293de241cabac71243", "type": "tool_call"}, {"name": "read_file", "args": {}, "id": "call_809386d45806498197f4e46d", "type": "tool_call"}],
    },
    {"type": "tool", "name": "read_file", "content": "[]\n"},
    {"type": "tool", "name": "read_file", "content": '{\n "refreshed_at": "2026-10-19T07:40:22+02:00",\n "refreshed_'},
    {"type": "ai", "content": "\n\n", "tool_calls": [{"name": "app_function_call", "args": {}, "id": "call_0bbf1c0c37854b94affb2324", "type": "tool_call"}]},
    {"type": "tool", "name": "app_function_call", "content": '{"error": "app function call failed: HTTP 502: {\\"detail\\":\\'},
    {"type": "ai", "content": "\n\n", "tool_calls": [{"name": "app_function_call", "args": {}, "id": "call_cabaae0e86ea4c6fa7024476", "type": "tool_call"}]},
    {"type": "tool", "name": "app_function_call", "content": "[TOOL_BLOCKED] Repeated internal errors — this tool is unava"},
    {"type": "ai", "content": "\n\n", "tool_calls": [{"name": "bash", "args": {}, "id": "call_d4d3c9031b0b445f942988a7", "type": "tool_call"}]},
    {"type": "tool", "name": "bash", "content": "app.js  app.json  banner.png  email-template.html  favicon.s"},
    {"type": "ai", "content": "\n\n", "tool_calls": [{"name": "bash", "args": {}, "id": "call_f26d325ea23a426b81b54263", "type": "tool_call"}]},
    {"type": "tool", "name": "bash", "content": '"Create Webfuse/Studio trials and retrieve magic signup link'},
    {"type": "ai", "content": "\n\n", "tool_calls": [{"name": "app_function_call", "args": {}, "id": "call_d95c8d6a425a43a989d80c78", "type": "tool_call"}]},
    {"type": "tool", "name": "app_function_call", "content": "[TOOL_BLOCKED] Repeated internal errors — this tool is unava"},
    {"type": "ai", "content": "", "tool_calls": [{"name": "bash", "args": {}, "id": "call_ea378baaa7744c93b92ecd7b", "type": "tool_call"}]},
    {"type": "tool", "name": "bash", "content": "108:  const res = await fetch(`/api/connectors/${CONNECTOR}/"},
    {"type": "ai", "content": "\n\n", "tool_calls": [{"name": "bash", "args": {}, "id": "call_18014c922aa841fb83963d8e", "type": "tool_call"}]},
    {"type": "tool", "name": "bash", "content": "7:const RECORDS_URL = `/api/app-data/v1/apps/${APP_SLUG}/col"},
    {"type": "ai", "content": "\n\n", "tool_calls": [{"name": "bash", "args": {}, "id": "call_8eaa95ab456044c985f3514a", "type": "tool_call"}]},
    {"type": "tool", "name": "bash", "content": "(no output)"},
    {"type": "ai", "content": "\n\n", "tool_calls": [{"name": "app_function_call", "args": {}, "id": "call_adcf4036add847978a8aec74", "type": "tool_call"}]},
    {"type": "tool", "name": "app_function_call", "content": "[TOOL_BLOCKED] Repeated internal errors — this tool is unava"},
    {"type": "ai", "content": "\n\n", "tool_calls": [{"name": "bash", "args": {}, "id": "call_7bb651006df5407e94d709d9", "type": "tool_call"}]},
    {"type": "tool", "name": "bash", "content": "JWT_PUBLIC_KEY=<redacted>\nBROWSER_USER_AGENT=<redacted>\nBROW"},
    {
        "type": "ai",
        "content": "\n"
        "\n"
        "The trial-manager `get_requests` function is returning server-side 500s and is now blocked for this run, so the cohort cannot be re-pulled. I will refresh the existing cohort (steps "
        "2–4) and flag this in the report.\n"
        "\n",
        "tool_calls": [
            {"name": "bq_query", "args": {}, "id": "call_e133f906807848b696af4c2e", "type": "tool_call"},
            {"name": "gmail_list_threads", "args": {}, "id": "call_f13f57da7d4e49988d01cfde", "type": "tool_call"},
            {"name": "gmail_list_threads", "args": {}, "id": "call_b867f630a2be419fb568848d", "type": "tool_call"},
            {"name": "gmail_list_threads", "args": {}, "id": "call_9911a8b122d3440cb7fded21", "type": "tool_call"},
            {"name": "gmail_list_threads", "args": {}, "id": "call_7a3689b1279b48d58d97f6cb", "type": "tool_call"},
        ],
    },
    {"type": "tool", "name": "gmail_list_threads", "content": '[{"type": "text", "text": "[\\n  {\\n    \\"thread_id\\": \\"1a13'},
    {"type": "tool", "name": "gmail_list_threads", "content": '[{"type": "text", "text": "[\\n  {\\n    \\"thread_id\\": \\"1a13'},
    {"type": "tool", "name": "gmail_list_threads", "content": '[{"type": "text", "text": "[\\n  {\\n    \\"thread_id\\": \\"1a13'},
    {"type": "tool", "name": "gmail_list_threads", "content": '[{"type": "text", "text": "[\\n  {\\n    \\"thread_id\\": \\"1a13'},
    {"type": "tool", "name": "bq_query", "content": '[{"type": "text", "text": "{\\"rows\\": [{\\"email\\": \\"paul.pr'},
    {
        "type": "ai",
        "content": "\n\n",
        "tool_calls": [
            {"name": "gmail_list_threads", "args": {}, "id": "call_b4d1db5fbdeb433d82dd2ffc", "type": "tool_call"},
            {"name": "gmail_list_threads", "args": {}, "id": "call_f8d35284c23a44369ea9505a", "type": "tool_call"},
            {"name": "gmail_list_threads", "args": {}, "id": "call_10459c850f354e4aaf7f6808", "type": "tool_call"},
        ],
    },
    {"type": "tool", "name": "gmail_list_threads", "content": '[{"type": "text", "text": "[\\n  {\\n    \\"thread_id\\": \\"1a13'},
    {"type": "tool", "name": "gmail_list_threads", "content": '[{"type": "text", "text": "[\\n  {\\n    \\"thread_id\\": \\"1a12'},
    {"type": "tool", "name": "gmail_list_threads", "content": '[{"type": "text", "text": "[\\n  {\\n    \\"thread_id\\": \\"1a12'},
    {"type": "ai", "content": "\n\n", "tool_calls": [{"name": "bash", "args": {}, "id": "call_1daf37beb78343cc8b6df490", "type": "tool_call"}]},
    {"type": "tool", "name": "bash", "content": "2026-10-19T11:42:24+02:00|19 Oct 2026, 11:42"},
    {"type": "ai", "content": "\n\n", "tool_calls": [{"name": "bash", "args": {}, "id": "call_da64954a2da145f9a30f82f0", "type": "tool_call"}]},
    {"type": "tool", "name": "bash", "content": "ok, rows: 7"},
    {"type": "ai", "content": "\n\n", "tool_calls": [{"name": "atlas_commit_app", "args": {}, "id": "call_ae5f907590e849dfa1fe6af3", "type": "tool_call"}]},
    {"type": "tool", "name": "atlas_commit_app", "content": '[{"type": "text", "text": "{\\n  \\"committed\\": true,\\n  \\"co'},
    {
        "type": "ai",
        "content": "\n"
        "\n"
        "FDE trials: no change (7 tracked, 4 no user yet, 3 signed up). All Gmail/BQ checks match the last run.\n"
        "Cohort pull failed: trial-manager get_requests returned 500 and is blocked this run, so new trials could not be detected.\n"
        "data.json refreshed_at updated and committed (24ed016).",
        "tool_calls": [],
    },
]


def _ai_texts(messages: list[dict]) -> list[str]:
    return [m["content"].strip() for m in messages if m["type"] == "ai" and m["content"].strip()]


@pytest.mark.parametrize("messages", [RUN_fe70a0cc, RUN_ddf9c4e4, RUN_7a42df3e], ids=["10-13", "10-14", "10-20"])
def test_the_lost_briefs_are_recovered_with_their_closing_note(messages):
    brief, note = _ai_texts(messages)
    final_text = messages[-1]["content"]
    recovered = _recover_closing_note_content({"messages": messages}, final_text)
    assert recovered == f"{brief}\n\n{note}"


@pytest.mark.parametrize("messages", [RUN_5a327e24, RUN_78babcd7], ids=["fde-5a327e24", "fde-78babcd7"])
def test_a_short_report_after_narration_is_left_alone(messages):
    # Earlier narration (230 and 215 chars) next to a tool call, then the real report.
    assert _recover_closing_note_content({"messages": messages}, messages[-1]["content"]) is None


def _turn(*messages: dict) -> dict:
    return {"messages": [{"type": "human", "content": "Produce the brief."}, *messages]}


BRIEF = "*Morning brief*\n\n*Inbox*\n" + "\n".join(f"• Item {i}: something that needs an action today" for i in range(6))


def _ai(content, *, tools: bool = False) -> dict:
    calls = [{"name": "write_file", "args": {}, "id": "call-1", "type": "tool_call"}] if tools else []
    return {"type": "ai", "content": content, "tool_calls": calls}


@pytest.mark.parametrize(
    "note",
    [
        "Nothing pending today.",  # no closing-note wording: a real (short) answer
        "Calendar updated: the 14:00 sync moved to 15:00.",
        "x" * 160 + " sent above.",  # longer than a closing note
        "Brief above.\nLog updated.\nTopics recorded.",  # three lines
    ],
)
def test_notes_that_are_not_closing_notes_are_delivered_as_they_are(note):
    result = _turn(_ai(BRIEF, tools=True), {"type": "tool", "name": "write_file", "content": "OK"}, _ai(note))
    assert _recover_closing_note_content(result, note) is None


def test_nothing_substantive_earlier_means_nothing_to_recover():
    result = _turn(_ai("\n\n", tools=True), {"type": "tool", "name": "write_file", "content": "OK"}, _ai("Brief delivered."))
    assert _recover_closing_note_content(result, "Brief delivered.") is None
    short = _turn(_ai("Checking the calendar and the inbox first.", tools=True), _ai("Brief delivered."))
    assert _recover_closing_note_content(short, "Brief delivered.") is None


def test_content_from_a_previous_turn_is_never_recovered():
    result = {"messages": [{"type": "human", "content": "yesterday"}, _ai(BRIEF), {"type": "human", "content": "today"}, _ai("Brief delivered above.")]}
    assert _recover_closing_note_content(result, "Brief delivered above.") is None


def test_hidden_control_messages_do_not_end_the_turn():
    hidden = {"type": "human", "content": "continue", "additional_kwargs": {"hide_from_ui": True}}
    result = _turn(_ai(BRIEF, tools=True), hidden, _ai("Brief delivered above."))
    assert _recover_closing_note_content(result, "Brief delivered above.") == f"{BRIEF.strip()}\n\nBrief delivered above."


def test_list_content_blocks_are_read():
    blocks = [{"type": "text", "text": BRIEF}]
    result = _turn(_ai(blocks, tools=True), _ai([{"type": "text", "text": "Sent above."}]))
    assert _recover_closing_note_content(result, "Sent above.") == f"{BRIEF.strip()}\n\nSent above."


def test_a_clarification_is_never_replaced():
    clarification = {"type": "tool", "name": "ask_clarification", "content": "Which repo?"}
    result = _turn(_ai(BRIEF, tools=True), clarification)
    assert _recover_closing_note_content(result, "Which repo?") is None


# ---------------------------------------------------------------------------
# Through the real delivery paths


async def _noop():
    return None


async def _stream(monkeypatch, tmp_path, *, messages: list[dict], unattended: bool):
    bridge = MemoryStreamBridge()
    record = RunRecord(run_id="7a42df3e", thread_id="cd6d75a3", assistant_id=None, status=RunStatus.success, on_disconnect=DisconnectMode.continue_)

    async def start_run(*args, **kwargs):
        await bridge.publish(record.run_id, "values", {"messages": messages})
        await bridge.publish_end(record.run_id)
        return record

    monkeypatch.setattr(thread_runs, "start_run", start_run)
    app = make_authed_test_app()
    app.include_router(thread_runs.router)
    app.state.stream_bridge = bridge
    app.state.run_manager = SimpleNamespace(get=AsyncMock(return_value=record))
    bus = MessageBus()
    manager = ChannelManager(bus=bus, store=JsonChannelStore(path=tmp_path / "channels.json"))
    inbound = InboundMessage(
        channel_name="telegram",
        chat_id="213839454",
        user_id="user-1",
        text="Good morning. Produce today's briefing for Telegram.",
        metadata={"message_id": "m1", "source": "playbook", "schedule_id": "morning-briefing"},
        unattended=unattended,
        report_url=REPORT_URL if unattended else None,
    )
    outbound = []
    bus.subscribe_outbound(lambda m: outbound.append(m) or _noop())
    with patch("app.channels.manager.report_delivery", new=AsyncMock(return_value=True)) as report:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test/api") as http:
            await manager._handle_streaming_chat(LangGraphClient(http), inbound, "cd6d75a3", "lead_agent", {}, {}, {"role": "user", "content": "brief"})
    return [m for m in outbound if m.is_final], report


@pytest.mark.asyncio
async def test_the_scheduled_telegram_brief_reaches_the_chat_and_chronos(monkeypatch, tmp_path):
    finals, report = await _stream(monkeypatch, tmp_path, messages=RUN_7a42df3e, unattended=True)
    brief, note = _ai_texts(RUN_7a42df3e)
    assert len(finals) == 1
    assert finals[0].text == f"{brief}\n\n{note}"
    assert [call.kwargs["status"] for call in report.await_args_list] == ["delivered"]
    assert report.await_args_list[0].kwargs["message_text"] == f"{brief}\n\n{note}"


@pytest.mark.asyncio
async def test_an_interactive_turn_is_unchanged(monkeypatch, tmp_path):
    finals, report = await _stream(monkeypatch, tmp_path, messages=RUN_7a42df3e, unattended=False)
    assert finals[0].text == RUN_7a42df3e[-1]["content"]
    report.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_non_streaming_path_recovers_the_brief_too(tmp_path):
    bus = MessageBus()
    manager = ChannelManager(bus=bus, store=JsonChannelStore(path=tmp_path / "channels.json"))
    client = SimpleNamespace(runs=SimpleNamespace(wait=AsyncMock(return_value={"messages": RUN_ddf9c4e4})))
    outbound = []
    bus.subscribe_outbound(lambda m: outbound.append(m) or _noop())
    inbound = InboundMessage(channel_name="slack", chat_id="C1", user_id="u1", text="brief", unattended=True, report_url=REPORT_URL)
    with patch("app.channels.manager.report_delivery", new=AsyncMock(return_value=True)) as report, patch.object(ChannelManager, "_channel_supports_streaming", return_value=False):
        await manager._handle_chat_on_thread(client, inbound, "e6a166e3")
    brief, note = _ai_texts(RUN_ddf9c4e4)
    assert [m.text for m in outbound] == [f"{brief}\n\n{note}"]
    assert report.await_args.kwargs["status"] == "delivered"


# ---------------------------------------------------------------------------
# A final text that is already silent stays silent (#34 wins over #151)

NARRATION = (
    "Checked the calendar for meetings starting in the next 15 to 20 minutes. The next one is the 14:00 product sync with "
    "Harel, which already had its prep note sent this morning, and nothing else starts before 15:30. Gmail shows no new "
    "threads from the attendees since then, so there is nothing to add."
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "final",
    [
        "No new meetings; prep for the 14:00 was already sent.",
        "Nothing new to report since the brief posted above.",
        "No updates. Staying silent, prep already sent.",
    ],
)
async def test_a_silent_poll_stays_silent_after_narration(monkeypatch, tmp_path, final):
    assert len(NARRATION) >= 250
    messages = [
        {"type": "human", "content": "Check my calendar for meetings starting in the next 15-20 minutes"},
        {"type": "ai", "content": NARRATION, "tool_calls": [{"name": "gmail_list_threads", "args": {}, "id": "call-1", "type": "tool_call"}]},
        {"type": "tool", "name": "gmail_list_threads", "content": "[]"},
        {"type": "ai", "content": final, "tool_calls": []},
    ]
    finals, report = await _stream(monkeypatch, tmp_path, messages=messages, unattended=True)
    assert finals == []
    assert [call.kwargs["status"] for call in report.await_args_list] == ["silent"]


@pytest.mark.asyncio
async def test_a_silent_poll_stays_silent_on_the_non_streaming_path(tmp_path):
    messages = [
        {"type": "human", "content": "poll"},
        {"type": "ai", "content": NARRATION, "tool_calls": [{"name": "gmail_list_threads", "args": {}, "id": "call-1", "type": "tool_call"}]},
        {"type": "tool", "name": "gmail_list_threads", "content": "[]"},
        {"type": "ai", "content": "No updates. Staying silent, prep already sent.", "tool_calls": []},
    ]
    bus = MessageBus()
    manager = ChannelManager(bus=bus, store=JsonChannelStore(path=tmp_path / "channels.json"))
    client = SimpleNamespace(runs=SimpleNamespace(wait=AsyncMock(return_value={"messages": messages})))
    outbound = []
    bus.subscribe_outbound(lambda m: outbound.append(m) or _noop())
    inbound = InboundMessage(channel_name="slack", chat_id="C1", user_id="u1", text="poll", unattended=True, report_url=REPORT_URL)
    with patch("app.channels.manager.report_delivery", new=AsyncMock(return_value=True)) as report, patch.object(ChannelManager, "_channel_supports_streaming", return_value=False):
        await manager._handle_chat_on_thread(client, inbound, "t-poll")
    assert outbound == []
    assert report.await_args.kwargs["status"] == "silent"

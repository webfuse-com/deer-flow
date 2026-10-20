"""[argus patch #153] A shell result is graded by its exit code, not by words in its output.

atlas-marko, 2026-10-20 06:28 CEST (run bca67986, seq 114): a node script exited
1 and echoed its own minified source, which contains ``mrr>500?'Medium'``. The
shared keyword rules read the word-bounded "500" as an internal error
(recoverable_by_model false, stop), ToolProgressMiddleware blocked bash for the
rest of the run, and the model retried bash 22 more times. The output of a
command is data the command printed; the tool itself worked.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import ToolMessage

from deerflow.agents.middlewares.tool_progress_middleware import ToolProgressMiddleware
from deerflow.agents.middlewares.tool_result_meta import (
    TOOL_META_KEY,
    TOOL_RESULT_ERROR_TYPES,
    normalize_tool_message,
)

# The exact tool result of run bca67986 seq 114 (deerflow_atlas_marko.run_events).
MARKO_NODE_FAILURE = (
    "ok\n"
    "<anonymous_script>:18\n"
    "function build(){var C=[];for(var i=0;i<P.length;i++){var pc=P[i];var bm=B.find(function(b){return b.n&&b.n.toLowerCase()===pc.name.toLowerCase()});var rb=RQ.find(function(x){retur"
    "n x.id===pc.id});var mrr=rb?rb.m:(bm?bm.m:(pc.mrr||0));var bk=bm?bm.b:(mrr>5000?'XL':mrr>1000?'Large':mrr>500?'Medium':'Small');var bq=bm?bm.q:0;var us=null;if(pc.id){var deps=D.fi"
    "lter(function(d){return d.p===pc.id});for(var j=0;j<deps.length;j++){var u=U.find(function(x){return x.s===deps[j].s&&x.c===String(deps[j].d)});if(u){us=u;break}}}var us2=su(us);va"
    "r sp=ss(pc.oc,pc.o3,pc.t9,bq);var fn=sf(bm?bm.c:0,bm?bm.ch:false,bk);var ct=sc2(pc.ar,rb?String(rb.t):pc.rt);var cr=srm(pc.h,pc.a);var tot=Math.round((us2.sc*W.us+sp.sc*W.sp+fn.sc*"
    "W.fn+ct.sc*W.ct)/100);C.push({name:pc.name,id:pc.id,mrr,bk,ind:bm?bm.i:null,cy:bm?bm.y:null,ow:bm?bm.o:null,sc:tot,us:us2,sp,fn,ct,cr,h:pc.h,a:pc.a,s7:us?us.s7:null,tr:us2.tr,uo:fa"
    "lse})}for(var k=0;k<U.length;k++){var u2=U[k];var dep=D.find(function(d){return d.s===u2.s&&d.d===parseInt(u2.c)});if(dep&&dep.p)continue;if(!u2.n)continue;var us3=su(u2);var sp2=s"
    "s(0,0,0,0);var fn2=sf(0,false,'Medium');var ct2=sc2(null,null);var cr2=srm(null,null);var tot2=us3.sc;C.push({name:u2.n,id:null,mrr:null,bk:null,ind:null,cy:null,ow:null,sc:tot2,us"
    ":us3,sp:sp2,fn:fn2,ct:ct2,cr:cr2,h:null,a:null,s7:u2.s7,tr:us3.tr,uo:true})}return C}\n"
    "                                                                                                                                                           ^\n"
    "\n"
    "ReferenceError: RQ is not defined\n"
    "    at build (eval at <anonymous> (/mnt/user-data/workspace/chs-refresh/sim.js:4:1), <anonymous>:18:156)\n"
    "    at eval (eval at <anonymous> (/mnt/user-data/workspace/chs-refresh/sim.js:4:1), <anonymous>:19:16)\n"
    "    at Object.<anonymous> (/mnt/user-data/workspace/chs-refresh/sim.js:4:1)\n"
    "    at Module._compile (node:internal/modules/cjs/loader:1706:14)\n"
    "    at Object..js (node:internal/modules/cjs/loader:1839:10)\n"
    "    at Module.load (node:internal/modules/cjs/loader:1441:32)\n"
    "    at Function._load (node:internal/modules/cjs/loader:1263:12)\n"
    "    at TracingChannel.traceSync (node:diagnostics_channel:328:14)\n"
    "    at wrapModuleLoad (node:internal/modules/cjs/loader:237:24)\n"
    "    at Function.executeUserEntryPoint [as runMain] (node:internal/modules/run_main:171:5)\n"
    "\n"
    "Node.js v22.21.0\n"
    "Exit Code: 1"
)


def _bash(content: str, *, name: str = "bash", status: str = "success") -> ToolMessage:
    return ToolMessage(content=content, tool_call_id="call_aeab0ba6342042ddb2db863e", name=name, status=status)


def _meta(msg: ToolMessage) -> dict[str, object]:
    return msg.additional_kwargs[TOOL_META_KEY]


def test_the_marko_failure_is_a_failed_command_not_an_internal_error():
    assert "mrr>500?'Medium'" in MARKO_NODE_FAILURE
    meta = _meta(normalize_tool_message(_bash(MARKO_NODE_FAILURE)))
    assert meta["status"] == "error"
    assert meta["exit_code"] == 1
    assert meta["error_type"] == "command_failed"
    assert meta["recoverable_by_model"] is True
    assert meta["recommended_next_action"] == "try_alternative"


def test_the_marko_failure_does_not_block_bash():
    mw = ToolProgressMiddleware(stagnation_threshold=3, warn_escalation_count=2, inject_assessment=True, jaccard_threshold=0.8, min_words=5)
    runtime = MagicMock()
    runtime.context = {"thread_id": "t-marko", "run_id": "bca67986"}
    request = SimpleNamespace(tool_call={"name": "bash", "id": "call_aeab0ba6342042ddb2db863e"}, runtime=runtime)
    mw.wrap_tool_call(request, lambda _r: normalize_tool_message(_bash(MARKO_NODE_FAILURE)))
    assert mw._phase_states["t-marko"]["bash"].phase != "blocked"
    # The next bash call (seq 116, the corrected script) reaches the sandbox.
    ran = []
    result = mw.wrap_tool_call(request, lambda _r: ran.append(True) or normalize_tool_message(_bash("rows: 412")))
    assert ran == [True]
    assert "[TOOL_BLOCKED]" not in str(result.content)


@pytest.mark.parametrize(
    "output",
    [
        "curl: (22) The requested URL returned error: 500",
        "HTTP/1.1 500 Internal Server Error",
        "Traceback (most recent call last):\nRuntimeError: unexpected error in parser",
        '{"status": 401, "message": "Unauthorized"}',
        "tesseract is not installed or it's not in your PATH",
        "Error: rate limit exceeded, retry later",
        "fatal: Authentication failed for 'https://github.com/surfly/cobro.git/'",
    ],
)
def test_keywords_in_command_output_do_not_grade_a_failed_command(output):
    meta = _meta(normalize_tool_message(_bash(f"{output}\nExit Code: 1")))
    assert meta["status"] == "error"
    assert meta["error_type"] == "command_failed"
    assert meta["recoverable_by_model"] is True
    assert meta["recommended_next_action"] != "stop"


@pytest.mark.parametrize(
    ("content", "error_type"),
    [
        # The shell's structured codes: found but not executable, not found.
        ("bash: ./run.sh: Permission denied\nExit Code: 126", "permission"),
        ("bash: tesseract: command not found\nExit Code: 127", "not_found"),
        # A timeout (the sandbox's hard timeout and timeout(1) both exit 124)
        # is a command the model can change, like any other failure.
        ("still building\nCommand timed out after 600 seconds.\nExit Code: 124", "command_failed"),
        ("Command exited with code 124", "command_failed"),
        ("Command exited with code 2", "command_failed"),
        ("partial output\nexit=3", "command_failed"),
    ],
)
def test_exit_codes_with_a_fixed_meaning_keep_it(content, error_type):
    meta = _meta(normalize_tool_message(_bash(content)))
    assert meta["status"] == "error"
    assert meta["error_type"] == error_type
    assert meta["recoverable_by_model"] is True
    assert meta["recommended_next_action"] != "stop"


def test_a_silent_timeout_after_failed_commands_does_not_block_bash():
    # Four failed commands and then `timeout 30 cmd` (coreutils prints nothing).
    mw = ToolProgressMiddleware(stagnation_threshold=3, warn_escalation_count=2, inject_assessment=True, jaccard_threshold=0.8, min_words=5)
    runtime = MagicMock()
    runtime.context = {"thread_id": "t-timeout", "run_id": "r1"}
    request = SimpleNamespace(tool_call={"name": "bash", "id": "tc-1"}, runtime=runtime)
    outputs = [f"step {i} failed\nExit Code: 1" for i in range(4)] + ["Command exited with code 124"]
    for output in outputs:
        result = mw.wrap_tool_call(request, lambda _r, output=output: normalize_tool_message(_bash(output)))
        assert "[TOOL_BLOCKED]" not in str(result.content)
    assert mw._phase_states["t-timeout"]["bash"].phase != "blocked"


@pytest.mark.parametrize(
    ("content", "exit_code"),
    [
        # An earlier marker-shaped line in the output is not the command's exit.
        ("exit=0\nrunning checks\nExit Code: 1", 1),
        ("Exit Code: 0\nnext stage\nCommand exited with code 2", 2),
        ("previous run: Exit Code: 3\nall good", None),
        ("Exit Code: 1\nretry succeeded", None),
    ],
)
def test_only_the_trailing_exit_marker_counts(content, exit_code):
    meta = _meta(normalize_tool_message(_bash(content)))
    assert meta.get("exit_code") == exit_code
    assert meta["status"] == ("error" if exit_code else "success")


def test_bash_tool_alias_is_graded_the_same_way():
    meta = _meta(normalize_tool_message(_bash(MARKO_NODE_FAILURE, name="bash_tool")))
    assert meta["error_type"] == "command_failed"


def test_a_clean_exit_with_500_in_its_output_is_a_success():
    meta = _meta(normalize_tool_message(_bash("served 500 requests, p95 500ms")))
    assert meta["status"] == "success"
    assert meta.get("exit_code") is None


def test_tool_level_errors_without_an_exit_code_keep_the_keyword_rules():
    # A sandbox that would not start is the tool failing, not the command.
    msg = _bash("Error: Unexpected error initializing sandbox: RuntimeError: Failed to inspect containers with docker", status="error")
    meta = _meta(normalize_tool_message(msg))
    assert meta["status"] == "error"
    assert meta["error_type"] == "internal"
    assert meta.get("exit_code") is None


def test_other_tools_still_grade_500_as_internal():
    msg = ToolMessage(content="Error: upstream returned HTTP 500", tool_call_id="tc-1", name="web_fetch", status="error")
    assert _meta(normalize_tool_message(msg))["error_type"] == "internal"


def test_command_failed_is_part_of_the_audit_vocabulary():
    assert "command_failed" in TOOL_RESULT_ERROR_TYPES


def test_repeated_failed_commands_get_a_command_hint():
    mw = ToolProgressMiddleware(stagnation_threshold=2, warn_escalation_count=2, inject_assessment=True, jaccard_threshold=0.8, min_words=5)
    runtime = MagicMock()
    runtime.context = {"thread_id": "t-hint", "run_id": "r1"}
    request = SimpleNamespace(tool_call={"name": "bash", "id": "tc-1"}, runtime=runtime)
    for _ in range(4):
        result = mw.wrap_tool_call(request, lambda _r: normalize_tool_message(_bash(MARKO_NODE_FAILURE)))
        assert "[TOOL_BLOCKED]" not in str(result.content)
    hints = [hint for queued in mw._pending.values() for hint in queued]
    assert any("The command keeps exiting with an error" in hint for hint in hints)

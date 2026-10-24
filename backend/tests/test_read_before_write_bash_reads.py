"""[argus patch #156] A pure-read bash call counts as a read for the write gate.

atlas-nicholas thread 4447dd29 (2026-10-23): the agent inspected a 1,891-line
connector with ``sed -n '203,230p'`` and friends, then wrote a three-part
``str_replace``. The gate blocked it ("you have not read its current version"),
which threw away a 97 s model call and forced a re-read. With
``read_before_write.bash_reads_mark`` the printed file is hashed under the gate
lock, like a ranged read_file.
"""

import asyncio
import hashlib
import posixpath
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

from deerflow.agents.middlewares.read_before_write_middleware import READ_MARKS_KEY, ReadBeforeWriteMiddleware
from deerflow.config.read_before_write_config import ReadBeforeWriteConfig
from deerflow.sandbox.command_classify import files_shown_by_inspection

CONNECTOR = "/mnt/projects/fde-avatar-demo/connector/fde_avatar.py"


@pytest.mark.parametrize(
    "command, shown",
    [
        # The thread's own inspection, verbatim.
        (
            "cd /mnt/projects/fde-avatar-demo && sed -n '203,230p' connector/fde_avatar.py && echo \"=== docstring doors ===\" && "
            "sed -n '18,28p' connector/fde_avatar.py && echo \"=== _link_secret ===\" && sed -n '/def _link_secret/,/^def /p' connector/fde_avatar.py | head -12",
            [CONNECTOR],
        ),
        (f"sed -n '1,120p' {CONNECTOR}", [CONNECTOR]),
        (f"sed -n '$p' {CONNECTOR}", [CONNECTOR]),
        ("cat /a.txt /b.txt", ["/a.txt", "/b.txt"]),
        ("head -n 40 /a.txt", ["/a.txt"]),
        ("tail -20 /a.txt", ["/a.txt"]),
        ("cd /mnt/projects && cd demo && cat a.py", ["/mnt/projects/demo/a.py"]),
        ("ls /mnt/projects && cat /mnt/projects/x/STATE.md | wc -l", ["/mnt/projects/x/STATE.md"]),
        # Not a read of the file's content.
        ("grep -n sign_link /a.py", []),
        ("wc -c /a.py", []),
        ("sed 's/x/y/' /a.py", []),
        ("sed -n 's/x/y/p' /a.py", []),
        ("sed -n -e '1,5p' /a.py", []),
        # Not pure-read.
        ("sed -i '1d' /a.py", []),
        ("cat /a.py > /b.py", []),
        ("cat $(ls /x)", []),
        ("python3 -c 'print(1)' && cat /a.py", []),
        ("echo $(cat /etc/hosts) && cat /a.py", []),
        ("echo x > /a.py && cat /a.py", []),
        # No known directory for a relative path.
        ("sed -n '1,5p' connector/a.py", []),
        ("cd connector && cat a.py", []),
        ("cd ~ && cat a.py", []),
    ],
)
def test_files_shown_by_inspection(command, shown):
    assert files_shown_by_inspection(command) == shown


def test_files_shown_is_capped():
    assert len(files_shown_by_inspection("cat " + " ".join(f"/f{i}" for i in range(20)))) == 8


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _request(name, args, messages=(), tool_call_id="call-1"):
    runtime = MagicMock()
    runtime.context = {"thread_id": "t-bash-reads"}
    return ToolCallRequest(tool_call={"name": name, "args": args, "id": tool_call_id}, tool=None, state={"messages": list(messages)}, runtime=runtime)


def _gate(files: dict[str, str], *, on: bool = True) -> ReadBeforeWriteMiddleware:
    def reader(_runtime, path):
        normalized = posixpath.normpath(path)
        if normalized not in files:
            raise FileNotFoundError(path)
        return files[normalized]

    return ReadBeforeWriteMiddleware(content_reader=reader, config=ReadBeforeWriteConfig(bash_reads_mark=on))


def _bash(gate, command, *, status="success", meta_status="success"):
    request = _request("bash", {"command": command, "description": "inspect"}, tool_call_id="b1")

    def handler(req):
        message = ToolMessage(content="...printed lines...", tool_call_id="b1", name="bash", status=status)
        message.additional_kwargs["deerflow_tool_meta"] = {"status": meta_status}
        return message

    return gate.wrap_tool_call(request, handler)


def _edit(gate, history):
    request = _request("str_replace", {"path": CONNECTOR, "old_str": "a", "new_str": "b", "description": "edit"}, history, tool_call_id="w1")
    return gate._check_write_gate(request)


def test_an_edit_after_a_sed_range_passes_the_gate():
    files = {CONNECTOR: "v1 source"}
    gate = _gate(files)
    shown = _bash(gate, "cd /mnt/projects/fde-avatar-demo && sed -n '203,230p' connector/fde_avatar.py")
    assert shown.additional_kwargs[READ_MARKS_KEY] == [{"path": CONNECTOR, "hash": _sha("v1 source")}]
    assert _edit(gate, [shown]) is None


def test_off_by_default_keeps_upstreams_rule():
    files = {CONNECTOR: "v1 source"}
    gate = ReadBeforeWriteMiddleware(content_reader=lambda _r, p: files[posixpath.normpath(p)])
    shown = _bash(gate, f"sed -n '1,9p' {CONNECTOR}")
    assert READ_MARKS_KEY not in shown.additional_kwargs
    assert _edit(gate, [shown]) is not None


def test_a_change_after_the_bash_read_still_blocks():
    files = {CONNECTOR: "v1 source"}
    gate = _gate(files)
    shown = _bash(gate, f"cat {CONNECTOR}")
    files[CONNECTOR] = "v2 source written by someone else"
    blocked = _edit(gate, [shown])
    assert blocked is not None and "not read its current version" in blocked.content


@pytest.mark.parametrize("status, meta_status", [("error", "error"), ("success", "error")])
def test_a_failed_bash_call_stamps_nothing(status, meta_status):
    gate = _gate({CONNECTOR: "v1"})
    shown = _bash(gate, f"cat {CONNECTOR}", status=status, meta_status=meta_status)
    assert READ_MARKS_KEY not in shown.additional_kwargs


def test_a_missing_file_is_left_out_and_others_are_marked():
    gate = _gate({"/a.txt": "A"})
    shown = _bash(gate, "cat /a.txt /missing.txt")
    assert shown.additional_kwargs[READ_MARKS_KEY] == [{"path": "/a.txt", "hash": _sha("A")}]


def test_non_reading_bash_is_untouched():
    gate = _gate({CONNECTOR: "v1"})
    shown = _bash(gate, f"grep -n x {CONNECTOR}")
    assert READ_MARKS_KEY not in shown.additional_kwargs
    assert _edit(gate, [shown]) is not None


def test_async_path_marks_too():
    files = {CONNECTOR: "v1 source"}
    gate = _gate(files)
    request = _request("bash", {"command": f"head -n 30 {CONNECTOR}"}, tool_call_id="b1")

    async def handler(req):
        message = ToolMessage(content="...", tool_call_id="b1", name="bash")
        message.additional_kwargs["deerflow_tool_meta"] = {"status": "success"}
        return message

    shown = asyncio.run(gate.awrap_tool_call(request, handler))
    assert shown.additional_kwargs[READ_MARKS_KEY] == [{"path": CONNECTOR, "hash": _sha("v1 source")}]
    assert _edit(gate, [shown]) is None

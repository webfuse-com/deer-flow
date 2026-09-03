"""[argus patch #81] Bash command classification for efficiency steering.

This module provides quote- and heredoc-aware compound-command splitting and
heuristics to classify bash commands as pure-read ("inspection") versus
state-modifying ("execution").

Efficiency steering, NOT a security boundary:
    This classifier is designed for efficiency and loop-detection steering (e.g.
    permitting repeated read-only inspection commands while throttling or warning
    on state-modifying actions), NOT as a security boundary or isolation
    mechanism. The sandbox itself is the security boundary. Evasion via tricks
    like piping to cat (``rm foo | cat``) or obfuscated subshells is an accepted,
    documented trade-off for simplicity and low false-positive rate on ordinary
    tool commands.
"""

import re
import shlex
from typing import Literal

# Pure-read binaries that produce stdout/stderr without modifying filesystem state.
_PURE_READ_BINARIES = frozenset(
    {
        "cat",
        "head",
        "tail",
        "grep",
        "sed",
        "ls",
        "find",
        "file",
        "stat",
        "diff",
        "wc",
        "sort",
        "uniq",
    }
)

# Subcommands of git that are strictly read-only inspection.
_GIT_INSPECTION_SUBCOMMANDS = frozenset(
    {
        "status",
        "log",
        "diff",
        "show",
    }
)

# Wrappers and prefixes that precede the actual command position.
_COMMAND_PREFIX_BINARIES = frozenset(
    {
        "env",
        "command",
        "builtin",
        "exec",
        "nohup",
        "time",
        "sudo",
        "doas",
    }
)

# A heredoc header and its delimiter: ``<<EOF``, ``<< EOF``, ``<<-EOF``,
# ``<<\EOF``, ``<<'EOF'``, ``<<"EOF"``. Both guards are needed to keep ``<<<``
# (a here-string, which has no body) from opening one: the lookahead rejects it
# at its first ``<``, and the lookbehind stops its trailing ``<<`` from matching
# one character later, where ``<<< "text"`` would otherwise read as a heredoc
# with delimiter ``text``.
_HEREDOC_HEADER = re.compile(r"(?<!<)<<(?!<)-?[ \t]*(?:\\?([A-Za-z_][\w.-]*)|'([^'\n]*)'|\"([^\"\n]*)\")")


def _consume_heredoc_bodies(command: str, pos: int, delimiters: list[str]) -> int:
    """Return the index just past the bodies of the *delimiters* opened so far.

    Bodies are consumed in the order their headers appeared, each running until a
    line whose stripped content equals its delimiter (``<<-`` strips leading tabs,
    which ``strip()`` covers). An unterminated body consumes the rest of the
    string: everything after the header genuinely is body, and there is no later
    statement to find.
    """
    for delimiter in delimiters:
        while pos < len(command):
            newline = command.find("\n", pos)
            if newline == -1:
                return len(command)
            line = command[pos:newline]
            pos = newline + 1
            if line.strip() == delimiter:
                break
        else:
            return len(command)
    return pos


def split_compound_command(command: str, *, split_pipes: bool = False) -> list[str]:
    """Split a compound command into sub-commands (quote-aware).

    Scans the raw command string so unquoted shell control operators are
    recognised even when they are not surrounded by whitespace
    (e.g. ``safe;rm -rf /`` or ``rm -rf /&&echo ok``). Operators inside
    quotes are ignored. If the command ends with an unclosed quote or a
    dangling escape, return the whole command unchanged (fail-closed —
    safer to classify the unsplit string than silently drop parts).

    Sequencing operators (``&&``, ``||``, ``;``) split, and so does an unquoted
    newline — it separates statements exactly like ``;``, so leaving it joined let
    ``echo hi\\n$(curl url)`` evade the anchored command-position rules that
    ``echo hi; $(curl url)`` triggers, despite identical shell semantics.

    A heredoc body is data, not statements: its newlines and operators are file
    content. Headers (``<<EOF``, ``<<-EOF``, ``<<'EOF'``) are therefore recorded
    as they are read and their bodies consumed verbatim at the newline that
    starts them, so a body line beginning with ``$(curl url)`` is not promoted to
    command position. ``<<<`` is a here-string, not a heredoc, and does not open
    one; neither does a ``<<`` inside ``$(( ... ))`` or ``(( ... ))``, where it is
    a bit shift whose right operand would otherwise read as a delimiter that never
    appears — swallowing the rest of the command. This is a heuristic, not shell
    parsing — the goal is only to avoid manufacturing command positions that the
    shell would never create, and to avoid destroying real ones.

    Pipes do not split by default, because a pipeline is one logical command.
    Pass ``split_pipes=True`` to also split on ``|``, which is what
    command-position detection needs — the word after a pipe starts a new
    command. Rules that span a pipe (``| sh``, ``base64 -d | ...``) are matched by
    the whole-command scan in :func:`_classify_command`, so they are unaffected by
    the extra split.
    """
    parts: list[str] = []
    current: list[str] = []
    pending_heredocs: list[str] = []
    in_single_quote = False
    in_double_quote = False
    arithmetic_depth = 0
    escaping = False
    index = 0

    while index < len(command):
        char = command[index]

        if escaping:
            current.append(char)
            escaping = False
            index += 1
            continue

        if char == "\\" and not in_single_quote:
            current.append(char)
            escaping = True
            index += 1
            continue

        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            current.append(char)
            index += 1
            continue

        if char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            current.append(char)
            index += 1
            continue

        if not in_single_quote and not in_double_quote:
            # ``<<`` inside arithmetic is a bit shift, not a redirection, and a
            # phantom header whose delimiter never appears would swallow the rest
            # of the command. Both ``$(( ... ))`` and the bare arithmetic command
            # ``(( ... ))`` are tracked. An unclosed ``((`` leaves the depth
            # positive, which only disables heredoc detection — newlines keep
            # splitting, so the failure direction stays towards seeing more
            # command positions rather than fewer.
            if char == "(" and command.startswith("((", index):
                arithmetic_depth += 1
                current.append("((")
                index += 2
                continue
            if arithmetic_depth and char == ")" and command.startswith("))", index):
                arithmetic_depth -= 1
                current.append("))")
                index += 2
                continue
            # A header can only start at ``<``; checking that first keeps the
            # regex off every other character of a long command.
            if char == "<" and not arithmetic_depth:
                heredoc = _HEREDOC_HEADER.match(command, index)
                if heredoc:
                    pending_heredocs.append(next(group for group in heredoc.groups() if group is not None))
                    current.append(heredoc.group(0))
                    index = heredoc.end()
                    continue
            if char == "\n":
                # The newline that follows a heredoc header is the statement
                # separator, and its body belongs to the statement being closed.
                if pending_heredocs:
                    body_end = _consume_heredoc_bodies(command, index + 1, pending_heredocs)
                    pending_heredocs = []
                    current.append(command[index:body_end])
                    index = body_end
                else:
                    index += 1
                part = "".join(current).strip()
                if part:
                    parts.append(part)
                current = []
                continue
            if command.startswith("&&", index) or command.startswith("||", index):
                part = "".join(current).strip()
                if part:
                    parts.append(part)
                current = []
                index += 2
                continue
            # Checked after "||" so a single "|" cannot steal that operator.
            if split_pipes and char == "|":
                part = "".join(current).strip()
                if part:
                    parts.append(part)
                current = []
                index += 1
                continue
            if char == ";":
                part = "".join(current).strip()
                if part:
                    parts.append(part)
                current = []
                index += 1
                continue

        current.append(char)
        index += 1

    # Unclosed quote or dangling escape → fail-closed, return whole command
    if in_single_quote or in_double_quote or escaping:
        return [command]

    part = "".join(current).strip()
    if part:
        parts.append(part)
    return parts if parts else [command]


def _has_unquoted_substitutions_or_output_redirection(cmd: str) -> bool:
    """Return True if cmd contains unquoted command/process substitution or writing output redirection.

    Allowed exceptions that stay inspection (no file write):
      - 2>/dev/null, 2>&1, 2>&-
    """
    in_single_quote = False
    in_double_quote = False
    escaping = False
    idx = 0
    n = len(cmd)

    while idx < n:
        c = cmd[idx]

        if escaping:
            escaping = False
            idx += 1
            continue

        if c == "\\" and not in_single_quote:
            escaping = True
            idx += 1
            continue

        if c == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            idx += 1
            continue

        if c == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            idx += 1
            continue

        if in_single_quote:
            idx += 1
            continue

        # In double quotes, backticks and $(...) are still command substitutions!
        if in_double_quote:
            if c == "`":
                return True
            if c == "$" and idx + 1 < n and cmd[idx + 1] == "(":
                return True
            idx += 1
            continue

        # Completely unquoted:
        if c == "`":
            return True
        if c == "$" and idx + 1 < n and cmd[idx + 1] == "(":
            return True
        if (c == "<" or c == ">") and idx + 1 < n and cmd[idx + 1] == "(":
            # Process substitution <(...) or >(...)
            return True

        if c == ">":
            # Output redirection candidate: >, >>, &>, >&, 1>, 2>, etc.
            # Look backwards from idx to see fd if immediately attached:
            # E.g. "2>" or "1>" or "&>"
            prefix_fd = None
            if idx > 0 and cmd[idx - 1] in "0123456789&":
                # Check how far back digits run
                k = idx - 1
                while k >= 0 and cmd[k] in "0123456789":
                    k -= 1
                if k >= 0 and cmd[k] == "&":
                    prefix_fd = cmd[k:idx]
                else:
                    prefix_fd = cmd[k + 1 : idx]

            # Look ahead for >>, >&, >|
            target_start = idx + 1
            op = ">"
            if target_start < n and cmd[target_start] in ">&|":
                op += cmd[target_start]
                target_start += 1

            # Skip optional whitespace between > operator and target
            while target_start < n and cmd[target_start] in " \t":
                target_start += 1

            target = cmd[target_start:]

            # Allowed exceptions:
            # 2>/dev/null, 2>&1, 2>&-
            is_allowed = False
            if prefix_fd == "2":
                if op == ">":
                    if target.startswith("/dev/null"):
                        rest = target[len("/dev/null") :]
                        if not rest or rest[0] in " \t;|&<>\n":
                            is_allowed = True
                elif op == ">&":
                    if target.startswith("1"):
                        rest = target[1:]
                        if not rest or rest[0] in " \t;|&<>\n":
                            is_allowed = True
                    elif target.startswith("-"):
                        rest = target[1:]
                        if not rest or rest[0] in " \t;|&<>\n":
                            is_allowed = True

            if not is_allowed:
                return True

            idx = target_start
            continue

        idx += 1

    return False


def _is_valid_env_var_name(name: str) -> bool:
    """Return True if name is a valid POSIX environment variable identifier."""
    if not name:
        return False
    if not (name[0].isalpha() or name[0] == "_"):
        return False
    return all(c.isalnum() or c == "_" for c in name[1:])


def _classify_subcommand(subcmd: str) -> Literal["inspection", "execution"]:
    """Classify a single subcommand (pipeline stage or sequential statement)."""
    # 1. Unquoted command substitution, process substitution, or writing output redirection
    if _has_unquoted_substitutions_or_output_redirection(subcmd):
        return "execution"

    # 2. Tokenize using shlex
    try:
        tokens = shlex.split(subcmd)
    except ValueError:
        return "execution"

    if not tokens:
        return "inspection"

    # 3. Strip leading environment assignments and wrapper commands
    idx = 0
    while idx < len(tokens):
        token = tokens[idx]
        if "=" in token:
            name, _ = token.split("=", 1)
            if _is_valid_env_var_name(name):
                idx += 1
                continue
        if token in _COMMAND_PREFIX_BINARIES:
            idx += 1
            continue
        break

    if idx >= len(tokens):
        # Empty after stripping prefixes (e.g. "FOO=bar" or "env")
        return "inspection"

    binary_token = tokens[idx]
    args = tokens[idx + 1 :]

    # Strip path from binary name if given (e.g. /bin/cat -> cat, /usr/bin/git -> git)
    binary = binary_token.rsplit("/", 1)[-1]

    # Handle `cd <dir>`: neutral directory changes write nothing
    if binary == "cd":
        return "inspection"

    # Pure-read check
    if binary not in _PURE_READ_BINARIES and binary != "git":
        return "execution"

    if binary == "git":
        # Must have a subcommand in inspection set {status, log, diff, show}
        # Flags might precede the subcommand, e.g. git --no-pager diff
        git_subcmd = None
        for arg in args:
            if arg.startswith("-"):
                continue
            git_subcmd = arg
            break
        if git_subcmd not in _GIT_INSPECTION_SUBCOMMANDS:
            return "execution"
        return "inspection"

    if binary == "sed":
        # Any in-place flag (-i, bundled short flags containing i like -ni, --in-place, --in-place=...) -> execution
        for arg in args:
            if arg == "--":
                break
            if arg.startswith("--in-place"):
                return "execution"
            if arg.startswith("-") and not arg.startswith("--"):
                if "i" in arg:
                    return "execution"
        return "inspection"

    if binary == "sort":
        # Any output flag (-o, bundled short flags containing o like -no, -oout, --output, --output=...) -> execution
        for arg in args:
            if arg == "--":
                break
            if arg.startswith("--output"):
                return "execution"
            if arg.startswith("-") and not arg.startswith("--"):
                if "o" in arg:
                    return "execution"
        return "inspection"

    if binary == "find":
        # Any of -delete, -exec, -execdir, -ok, -okdir, -fprintf, -fprint, -fprint0, -fls -> execution
        find_exec_flags = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprintf", "-fprint", "-fprint0", "-fls"}
        for arg in args:
            if arg in find_exec_flags:
                return "execution"
        return "inspection"

    return "inspection"


def classify_bash_command(cmd: str) -> Literal["inspection", "execution", "unknown"]:
    """Classify a bash command as inspection (pure read), execution, or unknown.

    Returns:
        - "unknown": empty or whitespace-only command.
        - "execution": unparseable or not verifiably pure-read.
        - "inspection": every subcommand across pipelines and compound operators
          is verifiably pure-read.
    """
    stripped = cmd.strip()
    if not stripped:
        return "unknown"

    # Split compound commands quote/heredoc-aware, splitting pipes too
    # so every pipeline stage is evaluated individually.
    subcommands = split_compound_command(cmd, split_pipes=True)

    # Fail-closed check: if the splitter returned the raw unclosed-quote command,
    # or an unclosed quote is detected, fail open to execution.
    if len(subcommands) == 1 and subcommands[0] == cmd:
        # Check if unclosed quote
        try:
            shlex.split(cmd)
        except ValueError:
            return "execution"

    for subcmd in subcommands:
        if not subcmd.strip():
            continue
        if _classify_subcommand(subcmd) == "execution":
            return "execution"

    return "inspection"

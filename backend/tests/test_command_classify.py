"""Tests for deerflow.sandbox.command_classify."""

from deerflow.sandbox.command_classify import (
    classify_bash_command,
    split_compound_command,
)


class TestSplitCompoundCommand:
    """Tests for split_compound_command public wrapper."""

    def test_basic_and_or_semicolon(self):
        assert split_compound_command("cmd1 && cmd2") == ["cmd1", "cmd2"]
        assert split_compound_command("cmd1 || cmd2") == ["cmd1", "cmd2"]
        assert split_compound_command("cmd1 ; cmd2") == ["cmd1", "cmd2"]

    def test_unclosed_quote_fail_closed(self):
        assert split_compound_command("echo 'unclosed") == ["echo 'unclosed"]

    def test_split_pipes(self):
        assert split_compound_command("cat log | grep ERR", split_pipes=True) == ["cat log", "grep ERR"]
        assert split_compound_command("cat log | grep ERR", split_pipes=False) == ["cat log | grep ERR"]


class TestClassifyBashCommand:
    """Tests for classify_bash_command."""

    def test_empty_and_whitespace_unknown(self):
        assert classify_bash_command("") == "unknown"
        assert classify_bash_command("   ") == "unknown"
        assert classify_bash_command("\n\t  \n") == "unknown"

    def test_unclosed_quote_fail_open_execution(self):
        assert classify_bash_command("grep 'unclosed") == "execution"
        assert classify_bash_command('cat "unclosed') == "execution"

    def test_pure_read_binaries(self):
        for cmd in [
            "cat file.txt",
            "head -n 10 file.txt",
            "tail -f log.txt",
            "grep -r 'pattern' src/",
            "sed -e 's/a/b/g' file.txt",
            "ls -la /tmp",
            "find . -name '*.py'",
            "file image.png",
            "stat file.txt",
            "diff file1.txt file2.txt",
            "wc -l file.txt",
            "sort names.txt",
            "uniq -c counts.txt",
        ]:
            assert classify_bash_command(cmd) == "inspection", f"Expected inspection for {cmd}"

    def test_sed_in_place_variants(self):
        # In-place flags should classify as execution
        assert classify_bash_command("sed -i 's/foo/bar/' f.txt") == "execution"
        assert classify_bash_command("sed -i.bak 's/foo/bar/' f.txt") == "execution"
        assert classify_bash_command("sed -ni 's/foo/bar/p' f.txt") == "execution"
        assert classify_bash_command("sed -in 's/foo/bar/p' f.txt") == "execution"
        assert classify_bash_command("sed --in-place 's/foo/bar/' f.txt") == "execution"
        assert classify_bash_command("sed --in-place=.bak 's/foo/bar/' f.txt") == "execution"
        # Pure filter sed should classify as inspection
        assert classify_bash_command("sed -n 's/foo/bar/p' f.txt") == "inspection"
        assert classify_bash_command("sed 's/foo/bar/' f.txt") == "inspection"

    def test_sort_variants(self):
        # Output flag writes a file -> execution
        assert classify_bash_command("sort -o out.txt in.txt") == "execution"
        assert classify_bash_command("sort --output=out.txt in.txt") == "execution"
        assert classify_bash_command("sort --output out.txt in.txt") == "execution"
        assert classify_bash_command("sort -no out.txt in.txt") == "execution"
        assert classify_bash_command("sort -oout.txt in.txt") == "execution"
        # Pure inspection sort
        assert classify_bash_command("sort -u in.txt") == "inspection"
        assert classify_bash_command("sort -n in.txt") == "inspection"

    def test_find_variants(self):
        # find with mutating or execution flags
        assert classify_bash_command("find . -name '*.tmp' -delete") == "execution"
        assert classify_bash_command("find . -name '*.tmp' -exec rm {} +") == "execution"
        assert classify_bash_command("find . -name '*.tmp' -exec rm {} \\;") == "execution"
        assert classify_bash_command("find . -name '*.tmp' -execdir rm {} \\;") == "execution"
        assert classify_bash_command("find . -name '*.tmp' -ok rm {} \\;") == "execution"
        assert classify_bash_command("find . -name '*.tmp' -okdir rm {} \\;") == "execution"
        assert classify_bash_command("find . -fprintf out.txt '%p\\n'") == "execution"
        assert classify_bash_command("find . -fprint out.txt") == "execution"
        assert classify_bash_command("find . -fprint0 out.txt") == "execution"
        assert classify_bash_command("find . -fls out.txt") == "execution"
        # Pure read find
        assert classify_bash_command("find . -name '*.py' -type f") == "inspection"

    def test_cd_neutral_and_compound(self):
        assert classify_bash_command("cd x && grep foo bar.txt") == "inspection"
        assert classify_bash_command("cd /tmp && ls -l") == "inspection"
        assert classify_bash_command("cd x && rm -rf y") == "execution"

    def test_env_prefixes(self):
        assert classify_bash_command("FOO=1 grep bar file.txt") == "inspection"
        assert classify_bash_command("env FOO=1 grep bar file.txt") == "inspection"
        assert classify_bash_command("command grep bar file.txt") == "inspection"
        assert classify_bash_command("builtin echo ok") == "execution"  # echo is not in pure-read set
        assert classify_bash_command("FOO=1 BAR=2 ls -l") == "inspection"
        assert classify_bash_command("sudo grep bar file.txt") == "inspection"

    def test_heredocs(self):
        # cat <<EOF is pure-read inspection
        heredoc_read = "cat <<EOF\nhello world\nEOF"
        assert classify_bash_command(heredoc_read) == "inspection"

        # cat > f <<EOF is execution via redirection
        heredoc_write = "cat > f <<EOF\nhello world\nEOF"
        assert classify_bash_command(heredoc_write) == "execution"

        # heredoc with quotes
        heredoc_quoted = "cat <<'EOF'\nhello world\nEOF"
        assert classify_bash_command(heredoc_quoted) == "inspection"

    def test_pipelines(self):
        assert classify_bash_command("cat log | grep ERR | sort -u") == "inspection"
        assert classify_bash_command("cat log | grep ERR") == "inspection"
        assert classify_bash_command("cat log | grep ERR | sort | uniq -c") == "inspection"
        # Pipeline with a non-pure-read binary
        assert classify_bash_command("cat log | grep ERR | tee output.txt") == "execution"
        assert classify_bash_command("cat log | grep ERR | awk '{print $1}'") == "execution"
        assert classify_bash_command("cat log | xargs rm") == "execution"

    def test_output_redirection(self):
        assert classify_bash_command("grep foo f > out") == "execution"
        assert classify_bash_command("grep foo f >> out") == "execution"
        assert classify_bash_command("grep foo f &> out") == "execution"
        assert classify_bash_command("grep foo f >& out") == "execution"
        assert classify_bash_command("cat f >out") == "execution"
        assert classify_bash_command("cat f 1>out") == "execution"
        assert classify_bash_command("cat f 2>out.err") == "execution"
        assert classify_bash_command("cat f >/dev/null") == "execution"

        # Allowed exceptions: stderr sinks/fd dups
        assert classify_bash_command("grep foo f 2>/dev/null") == "inspection"
        assert classify_bash_command("grep foo f 2>&1") == "inspection"
        assert classify_bash_command("grep foo f 2>&-") == "inspection"
        assert classify_bash_command("find . -name '*.py' 2>/dev/null") == "inspection"

    def test_git_subcommands(self):
        assert classify_bash_command("git status") == "inspection"
        assert classify_bash_command("git log -n 5") == "inspection"
        assert classify_bash_command("git diff HEAD~1") == "inspection"
        assert classify_bash_command("git show HEAD") == "inspection"
        assert classify_bash_command("git --no-pager diff") == "inspection"

        # Non-inspection git subcommands
        assert classify_bash_command("git commit -m 'msg'") == "execution"
        assert classify_bash_command("git push") == "execution"
        assert classify_bash_command("git checkout main") == "execution"
        assert classify_bash_command("git reset --hard") == "execution"
        assert classify_bash_command("git clean -fd") == "execution"

    def test_command_substitution_and_process_substitution(self):
        assert classify_bash_command("grep $(cat file.txt) other.txt") == "execution"
        assert classify_bash_command("grep `cat file.txt` other.txt") == "execution"
        assert classify_bash_command("diff <(sort file1) <(sort file2)") == "execution"
        assert classify_bash_command("grep foo >(tee log)") == "execution"
        # Quoted substitution in single quotes is literal, not substitution
        assert classify_bash_command("grep '$(cat file.txt)' other.txt") == "inspection"

    def test_compound_mixed(self):
        assert classify_bash_command("cat f && rm g") == "execution"
        assert classify_bash_command("cat f ; echo done") == "execution"
        assert classify_bash_command("cat f || ls") == "inspection"
        assert classify_bash_command("ls -la && grep foo f.txt") == "inspection"

    def test_non_read_binaries(self):
        assert classify_bash_command("xargs") == "execution"
        assert classify_bash_command("sh script.sh") == "execution"
        assert classify_bash_command("bash -c 'ls'") == "execution"
        assert classify_bash_command("python -c 'print(1)'") == "execution"
        assert classify_bash_command("python3 script.py") == "execution"
        assert classify_bash_command("awk '{print $1}' f.txt") == "execution"
        assert classify_bash_command("perl -pe 's/a/b/' f.txt") == "execution"
        assert classify_bash_command("tee file.txt") == "execution"
        assert classify_bash_command("echo hello") == "execution"
        assert classify_bash_command("rm -rf /") == "execution"
        assert classify_bash_command("touch file.txt") == "execution"

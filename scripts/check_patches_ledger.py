#!/usr/bin/env python3
"""check_patches_ledger.py: keep PATCHES.md consistent with itself and with the commits.

PATCHES.md's hard rule: a patch that is not in this file does not exist. The
CI gate that enforced it matched only the literal commit subject
"[argus] patch #NN"; the commit convention moved on in August 2026 and eleven
patches landed with no ledger entry, one number was assigned twice, and one
was never assigned. This script is the rule made mechanical.

Checks (stdlib only, exit 1 on any failure):
  1. No patch number appears in two section headers ("## Patch #N",
     "## Patch #N/#M", "## Reverted patch #N").
  2. Every number in the table of contents has a section, and every section
     has a table-of-contents row.
  3. With --range A..B: every "patch #N" named in a commit subject or body in
     that range has a section (case-insensitive, any prefix).
  4. With --changed-files FILE and --pr-body FILE: if a changed path is under
     backend/ or frontend/ (excluding tests and *.md) then PATCHES.md must be
     among the changed files, unless the PR body carries a line
     "Ledger: none (<reason>)", which is printed so the exception is auditable.

Usage: scripts/check_patches_ledger.py [--ledger PATCHES.md] [--range origin/argus..HEAD]
                                       [--changed-files list.txt] [--pr-body body.txt]
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

HEADER_RE = re.compile(r"^## (?:Reverted )?[Pp]atch (.+?)\s*$", re.M)
TOC_RE = re.compile(r"^\| \[([^\]]+)\]\(#[^)]*\)", re.M)
NUM_RE = re.compile(r"#(\d+)")
MENTION_RE = re.compile(r"\bpatch #(\d+)", re.I)
LEDGER_NONE_RE = re.compile(r"^\s*Ledger:\s*none\s*\((.+)\)\s*$", re.I | re.M)
CODE_RE = re.compile(r"^(backend|frontend)/(?!tests/)(?!.*\.md$).+")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", default="PATCHES.md", type=Path)
    ap.add_argument("--range", dest="rev_range", default=None)
    ap.add_argument("--changed-files", type=Path, default=None)
    ap.add_argument("--pr-body", type=Path, default=None)
    args = ap.parse_args()

    text = args.ledger.read_text()
    errors: list[str] = []

    headers = HEADER_RE.findall(text)
    section_nums: list[str] = []
    for h in headers:
        section_nums += NUM_RE.findall(h)
    dupes = sorted((n for n, c in Counter(section_nums).items() if c > 1), key=int)
    if dupes:
        errors.append(f"patch number(s) used by more than one section: {', '.join('#' + d for d in dupes)}")

    toc_nums: list[str] = []
    for label in TOC_RE.findall(text):
        toc_nums += NUM_RE.findall(label)
    sec_set, toc_set = set(section_nums), set(toc_nums)
    missing_sections = sorted(toc_set - sec_set, key=int)
    missing_rows = sorted(sec_set - toc_set, key=int)
    if missing_sections:
        errors.append(f"table of contents lists patches with no section: {', '.join('#' + n for n in missing_sections)}")
    if missing_rows:
        errors.append(f"sections with no table-of-contents row: {', '.join('#' + n for n in missing_rows)}")

    if args.rev_range:
        log = subprocess.run(["git", "log", "--format=%s%n%b", args.rev_range],
                             capture_output=True, text=True, check=False)
        if log.returncode != 0:
            errors.append(f"git log {args.rev_range} failed: {log.stderr.strip()}")
        else:
            mentioned = sorted(set(MENTION_RE.findall(log.stdout)), key=int)
            unrecorded = [n for n in mentioned if n not in sec_set]
            if unrecorded:
                errors.append(
                    f"commits in {args.rev_range} name patch(es) with no PATCHES.md section: "
                    f"{', '.join('#' + n for n in unrecorded)}. Add the section in the same PR (the hard rule)."
                )

    if args.changed_files is not None:
        changed = [l.strip() for l in args.changed_files.read_text().splitlines() if l.strip()]
        code = [c for c in changed if CODE_RE.match(c)]
        if code and "PATCHES.md" not in changed:
            body = args.pr_body.read_text() if args.pr_body and args.pr_body.is_file() else ""
            m = LEDGER_NONE_RE.search(body)
            if m:
                print(f"check_patches_ledger: code changed without a ledger update; declared exception: {m.group(1).strip()}")
            else:
                errors.append(
                    "this change touches backend/ or frontend/ code but does not update PATCHES.md and the PR body "
                    "has no 'Ledger: none (<reason>)' line. Either record the patch or state why it is not one."
                )

    if errors:
        for e in errors:
            print(f"::error::{e}" if "GITHUB_ACTIONS" in __import__("os").environ else f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"check_patches_ledger: {len(headers)} sections, {len(toc_set)} numbered TOC entries, consistent")
    return 0


if __name__ == "__main__":
    sys.exit(main())

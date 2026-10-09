#!/usr/bin/env python3
"""Check the design sections every managed repo's CLAUDE.md files name against the current
handbook/design.md, and keep one issue open while any is dead (design 6.8; handbook#80).

A repo's own PR check (pr-title / claude-md) can't read the private handbook, and renumbering the
design never runs another repo's CI, so this runs daily from the claude-md-check workflow. It
checks design sections only (`design 6.8`, `section 4.2`), with claude_md.py's rules; paths and
`enforced by:` names are checked on every PR already.

Usage:
  scripts/design_refs.py                          check; prints each dead reference and exits 1
                                                  if any (0 if none, 2 if a repo can't be read)
  scripts/design_refs.py --report <file> <code>   open, update or close the issue from a check's
                                                  output and exit code

The check needs a token that can read every managed repo's contents (the read-only settings app);
--report needs one that can write this repo's issues (the automation app). The issue is a Task in
this repo with the `claude-md-drift` source label, assigned to the automation owners
(scripts/one_issue.py).
"""

import sys

from claude_md import section_problems, sections_in
from one_issue import keep
from rules import ORG, managed_repos, read_file, repo_files

REPO = f"{ORG}/.github"
LABEL = "claude-md-drift"
TITLE = "A CLAUDE.md names a design section that no longer exists"
DESIGN = ("handbook", "design.md")


def claude_md_paths(files):
    """Every CLAUDE.md in a repo's file list, outside node_modules (as the PR check skips it)."""
    return sorted(p for p in files if p.split("/")[-1] == "CLAUDE.md"
                  and "node_modules" not in p.split("/"))


def dead_references(repo, path, text, sections):
    """'repo/path: line N: design section X does not exist.' for each dead reference."""
    return [f"{repo}/{path}: {p}" for p in section_problems(text, sections)]


def check(read=read_file, files=repo_files, repos=None):
    """(dead references, repos that couldn't be read). `read` and `files` are rules.read_file and
    rules.repo_files, replaced in tests."""
    design = read(*DESIGN)
    if design is None:
        return [], [DESIGN[0]]
    sections = sections_in(design)
    found, unreadable = [], []
    for repo in repos or managed_repos():
        listed = files(repo)
        if listed is None:
            unreadable.append(repo)
            continue
        for path in claude_md_paths(listed):
            text = read(repo, path)
            if text is None:
                unreadable.append(f"{repo}/{path}")
                continue
            found += dead_references(repo, path, text, sections)
    return found, unreadable


def problems(output):
    """The '  - ...' lines a check printed."""
    return [l.strip()[2:] for l in output.splitlines() if l.startswith("  - ")]


def body(output):
    found = "\n".join(f"- `{p.split(':', 1)[0]}`:{p.split(':', 1)[1]}" for p in problems(output)) \
        or "- (no details; see the run log)"
    return f"""### What

The daily `CLAUDE.md` check found lines naming design sections that `handbook/design.md` no longer
has:

{found}

### Why

Every `CLAUDE.md` line points at something that exists (design 6.8). A repo's own PR check can't
read the private handbook, and a renumbered design never runs another repo's CI, so this daily
check (`.github/workflows/claude-md-check.yml`) reports them here.

### Done when

- Each line names the right section again (a PR in that repo), or the design gets the section back
- The next daily check closes this issue

### Discipline

Software

### Priority

Medium

### Links

Design 6.8; handbook#80; `.github/scripts/design_refs.py`

### Design decisions

_No response_"""


def main():
    if sys.argv[1:2] == ["--report"]:
        if len(sys.argv) != 4:
            sys.exit(__doc__)
        output, code = open(sys.argv[2]).read(), int(sys.argv[3])
        if code not in (0, 1):
            sys.exit(f"the check itself failed (exit {code}); see the log above")
        print(keep(REPO, LABEL, TITLE, body(output) if code == 1 else None))
        return
    if len(sys.argv) != 1:
        sys.exit(__doc__)
    found, unreadable = check()
    if unreadable:
        print("Can't read (the token needs contents access to every managed repo): "
              + ", ".join(unreadable))
        sys.exit(2)
    if found:
        print("FAIL: design sections named in CLAUDE.md files that the design no longer has:")
        for p in found:
            print(f"  - {p}")
        sys.exit(1)
    print("PASS: every design section named in a CLAUDE.md exists")


if __name__ == "__main__":
    main()

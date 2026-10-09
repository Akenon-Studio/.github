#!/usr/bin/env python3
"""Turn a verify-settings.py run into one open Task issue in this repo (design 6.2).

Usage: scripts/report_drift.py <verify output file> <verify exit code>
Exit code 1 (drift): open the `settings-drift` issue, or update it if the problems changed.
Exit code 0: close any open one with a comment. Anything else: fail, since the check itself broke.
The issue body is a filled-in Task form, so the issue-fields automation puts it on the board. It is
assigned to the automation owners (rulesets/teams.json, design 6.8; scripts/one_issue.py), so the
drift reaches a person; an update re-adds them if nobody is assigned.
"""

import sys

from one_issue import keep
from rules import ORG

REPO = f"{ORG}/.github"
LABEL = "settings-drift"
TITLE = "Live GitHub settings differ from rulesets/"


def problems(output):
    """The '  - ...' lines verify-settings.py prints after FAIL."""
    return [l.strip()[2:] for l in output.splitlines() if l.startswith("  - ")]


def notes(output):
    return [l[len("note: "):] for l in output.splitlines() if l.startswith("note: ")]


def body(output):
    found = "\n".join(f"- {p}" for p in problems(output)) or "- (no details; see the run log)"
    extra = notes(output)
    also = ("\n\nAlso noted (not failures):\n" + "\n".join(f"- {n}" for n in extra)) if extra else ""
    return f"""### What

The daily settings check found live GitHub settings that differ from `rulesets/`:

{found}{also}

### Why

The rules are code (design 6.2); anything changed by hand in the GitHub UI must be put back, or
the change made properly in a PR to `rulesets/`.

### Done when

- An owner runs `GH=/opt/homebrew/bin/gh scripts/apply-rules.py` (or `apply-board.py`), or a PR
  changes `rulesets/` to match a deliberate change
- `scripts/verify-settings.py` passes; the next daily check closes this issue

### Discipline

Software

### Priority

High

### Links

Design 6.2; `.github/workflows/settings-check.yml`

### Design decisions

_No response_"""


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    output = open(sys.argv[1]).read()
    code = int(sys.argv[2])
    if code not in (0, 1):
        sys.exit(f"verify-settings.py itself failed (exit {code}); see the log above")
    print(keep(REPO, LABEL, TITLE, body(output) if code == 1 else None))


if __name__ == "__main__":
    main()

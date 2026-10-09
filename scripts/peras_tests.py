#!/usr/bin/env python3
"""Report the result of running peras's agent-hook tests against this repo's main (handbook#119).

peras's tests (core/workspace/tests) use this repo's scripts (issue_fields.py, board.py, rules.py),
so a change here can break them. A PR here can't read the private peras repo (the key that can is
limited to main), so the peras-tests workflow runs them right after every change lands on main, and
this script keeps one open `peras-tests-broken` Task issue while they fail, assigned to the
automation owners, and closes it when they pass again (scripts/one_issue.py).

Usage: scripts/peras_tests.py --report <test output file> <exit code>
"""

import sys

from one_issue import keep
from rules import ORG

REPO = f"{ORG}/.github"
LABEL = "peras-tests-broken"
TITLE = "A change to .github's main broke peras's agent-hook tests"
TAIL_LINES = 40


def body(output):
    tail = "\n".join(output.rstrip().splitlines()[-TAIL_LINES:]) or "(no output; see the run log)"
    return f"""### What

peras's agent-hook tests (`core/workspace/tests`) fail against `.github`'s current main. The last
lines of the run:

```
{tail}
```

### Why

peras's hooks and tests use this repo's scripts, so a change here can break them
(`.github/workflows/peras-tests.yml`, handbook#119).

### Done when

- peras's tests pass against `.github` main again (fix the script here, or peras's tests and fake
  GitHub there), and the next run closes this issue

### Discipline

Software

### Priority

High

### Links

Akenon-Studio/handbook#119; `.github/scripts/peras_tests.py`

### Design decisions

design 6.8"""


def main():
    if sys.argv[1:2] != ["--report"] or len(sys.argv) != 4:
        sys.exit(__doc__)
    output, code = open(sys.argv[2]).read(), int(sys.argv[3])
    print(keep(REPO, LABEL, TITLE, body(output) if code != 0 else None,
               passed="peras's tests pass against main again. Closing."))


if __name__ == "__main__":
    main()

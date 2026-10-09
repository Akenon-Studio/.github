#!/usr/bin/env python3
"""Check that a PR opened by a person has its author among the assignees (design 6.8, "Every
hand-off goes to an assigned human"). A PR a bot or app opens is exempt here: its reviewer comes
from the automation owners team. An agent outside Peras works as its human's GitHub login, so this
makes it assign exactly that human (`gh pr create --assignee @me`).

Usage: scripts/pr_author_assigned.py   (reads PR_AUTHOR, PR_AUTHOR_TYPE and PR_ASSIGNEES, a JSON
list of the PR's assignees as the event gives them, from the environment)
In Actions it runs as the `author-assigned` job of the reusable pr-title workflow, which callers
also run on `assigned` and `unassigned`, so assigning the author runs it again.
"""

import json
import os
import sys


def problems(author, author_type, assignees):
    """What is wrong, as sentences. Empty means it passes. `assignees` are logins."""
    if author_type == "Bot" or not author:
        return []
    if author.lower() in {a.lower() for a in assignees}:
        return []
    return [f"The PR's author, @{author}, is not an assignee. Assign @{author} (Assignees, in the "
            "sidebar; or open PRs with `gh pr create --assignee @me`). Whoever opens a PR is "
            "assigned to it, so a hand-off reaches a person (design 6.8)."]


def main():
    assignees = [a["login"] for a in json.loads(os.environ.get("PR_ASSIGNEES") or "[]")]
    author, author_type = os.environ.get("PR_AUTHOR", ""), os.environ.get("PR_AUTHOR_TYPE", "")
    found = problems(author, author_type, assignees)
    for p in found:
        print(f"::error::{p}")
    if found:
        print("Assigning the author runs this check again. Re-running the job does not help: it "
              "reads the assignees the PR had when the run started.")
        sys.exit(1)
    if author_type == "Bot":
        print(f"OK: opened by {author}, a bot; its reviewer comes from the automation owners")
    else:
        print(f"OK: opened by {author}, who is assigned")


if __name__ == "__main__":
    main()

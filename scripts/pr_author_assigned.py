#!/usr/bin/env python3
"""Check that a PR opened by a person has its author among the assignees (design 6.8, "Every
hand-off goes to an assigned human"). A PR a bot or app opens is exempt here: its reviewer comes
from the automation owners team. An agent outside Peras works as its human's GitHub login, so this
makes it assign exactly that human (`gh pr create --assignee @me`).

Usage: scripts/pr_author_assigned.py   (reads PR_AUTHOR, PR_AUTHOR_TYPE and PR_ASSIGNEES, a JSON
list of the PR's assignees as the event gives them, from the environment)
In Actions it runs as the `author-assigned` job of the reusable pr-title workflow, which callers
also run on `assigned` and `unassigned`, so assigning the author runs it again.

The event's assignees are a snapshot from when the run started, and `gh pr create --assignee`
assigns a moment after the PR opens (handbook#104). So when the snapshot lacks the author, the
script asks GitHub for the PR's current assignees (GITHUB_REPOSITORY, PR_NUMBER, GITHUB_TOKEN),
a few times over some seconds, before failing.
"""

import json
import os
import sys
import time
import urllib.request

RETRIES, WAIT_SECONDS = 4, 5


def current_assignees(repo, number, token):
    """The PR's assignees now, from the API."""
    request = urllib.request.Request(f"https://api.github.com/repos/{repo}/pulls/{number}",
                                     headers={"Authorization": f"Bearer {token}",
                                              "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return [a["login"] for a in json.load(response)["assignees"]]


def check(author, author_type, assignees, fetch=None, retries=RETRIES, wait=WAIT_SECONDS,
          sleep=time.sleep):
    """problems(), re-reading the assignees with `fetch` while the snapshot lacks the author."""
    found = problems(author, author_type, assignees)
    for _ in range(retries if fetch else 0):
        if not found:
            break
        sleep(wait)
        found = problems(author, author_type, fetch())
    return found


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
    repo, number, token = (os.environ.get(k) for k in ("GITHUB_REPOSITORY", "PR_NUMBER",
                                                       "GITHUB_TOKEN"))
    fetch = (lambda: current_assignees(repo, number, token)) if repo and number and token else None
    found = check(author, author_type, assignees, fetch)
    for p in found:
        print(f"::error::{p}")
    if found:
        print("Assigning the author runs this check again.")
        sys.exit(1)
    if author_type == "Bot":
        print(f"OK: opened by {author}, a bot; its reviewer comes from the automation owners")
    else:
        print(f"OK: opened by {author}, who is assigned")


if __name__ == "__main__":
    main()

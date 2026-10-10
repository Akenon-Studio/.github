#!/usr/bin/env python3
"""Keep the board Status of the issues a PR closes in step with the PR (design 6.6).

Usage: scripts/pr_status.py <event.json>   (a pull_request_target event; issue-fields.yml runs it)

- opened, reopened, edited or ready for review, and not a draft: each open issue the PR closes goes
  to In review.
- turned back into a draft, or closed without merging: each one still in In review goes back to In
  progress if someone is assigned, else Todo.
- closed by a merge: nothing (the merge closes the issue, and the board sets Done).

The issues are the PR's closing references (its `Closes #n` lines), only in repos the PR's author
can write to: the text is theirs and the app's token reaches every repo. A fork's PR changes
nothing. Runs as the org app.
"""

import json
import os
import sys

from board import find_board_fields
from issue_fields import board_item, graphql, load_issue, set_field
from rules import try_gh

IN_REVIEW = "In review"
CLOSING = """query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) { pullRequest(number: $number) {
    closingIssuesReferences(first: 50) { nodes { number state repository { nameWithOwner } } } } } }"""


def wanted(event):
    """'review' (to In review), 'back' (out of it) or None, for a pull_request_target event."""
    pr, action = event["pull_request"], event["action"]
    if (pr.get("head", {}).get("repo") or {}).get("full_name") != event["repository"]["full_name"]:
        return None  # a fork
    if action in ("opened", "reopened", "edited", "ready_for_review"):
        return None if pr.get("draft") else "review"
    if action == "converted_to_draft" or (action == "closed" and not pr.get("merged")):
        return "back"
    return None


def can_write(repo, login, author_type, pr_repo):
    """Whether the PR's author may write to `repo` (a bot: only its own repo)."""
    if author_type == "Bot":
        return repo == pr_repo
    level = try_gh(f"repos/{repo}/collaborators/{login}/permission") or {}
    return level.get("permission") in ("admin", "maintain", "write")


def new_status(move, status, assigned):
    """The Status to set, or None to leave it."""
    if move == "review":
        return IN_REVIEW if status not in (IN_REVIEW, "Done") else None
    if status == IN_REVIEW:
        return "In progress" if assigned else "Todo"
    return None


def main(path):
    event = json.load(open(path))
    move = wanted(event)
    pr, repo = event["pull_request"], event["repository"]["full_name"]
    if not move:
        print(f"#{pr['number']} {event['action']}: nothing to change")
        return 0
    owner, name = repo.split("/")
    refs = graphql(CLOSING, owner=owner, name=name, number=pr["number"])["repository"]["pullRequest"]
    board = None
    for ref in refs["closingIssuesReferences"]["nodes"]:
        if not ref or ref["state"] != "OPEN":  # None: an issue this token can't see
            continue
        target = ref["repository"]["nameWithOwner"]
        where = f"{target}#{ref['number']}"
        if not can_write(target, pr["user"]["login"], pr["user"].get("type"), repo):
            print(f"{where}: left alone ({pr['user']['login']} can't write to {target})")
            continue
        board = board or find_board_fields("Akenon Studio")
        issue = load_issue(target, ref["number"])
        item, status = board_item(board, issue)
        status_to = new_status(move, status, bool(issue["assignedActors"]["nodes"]))
        if status_to:
            set_field(board, item, "Status", status_to)
            print(f"{where}: {status} -> {status_to}")
        else:
            print(f"{where}: stays {status}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1]))

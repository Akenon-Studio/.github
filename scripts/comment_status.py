#!/usr/bin/env python3
"""Waiting for human from an issue's own comments (design 6.6, 6.8).

Usage: scripts/comment_status.py <event.json>   (an issue_comment event; issue-fields.yml runs it)

A comment with a line starting `Waiting on` (a progress comment saying the work now waits on a
person, e.g. `Waiting on: @partner to choose the supplier`) sets the issue to Waiting for human and
assigns anyone that line @-mentions. `Waiting on: nothing` (or none, no one) doesn't count. A later
comment without such a line (the person's answer, or the next progress comment) takes an issue in
Waiting for human back to In progress, or Todo if nobody is assigned. Comments on PRs, by bots, or on
closed issues change nothing, nor do comments from outside the org or edits. A `Waiting on` line moves
only Todo or In progress: In review, Blocked and Done stay. Runs as the org app.
"""

import json
import re
import sys

from board import find_board_fields
from issue_fields import WAITING, board_item, load_issue, set_field
from rules import gh

WAITING_LINE = re.compile(r"^ {0,3}(?:[-*+][ \t]+)?[*_]*waiting on\b[*_]*(.*)$", re.I | re.M)  # 4 spaces: code
NOT_WAITING = re.compile(r"^[\s:*_–-]*(nothing|none|no one|nobody|n/?a)\b|^[\s:*_–-]*$", re.I)
MENTION = re.compile(r"(?<![\w/@])@([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))\b")
# Fenced code (``` or ~~~, closed or running to the end), inline code and HTML comments
QUOTED = re.compile(r"```.*?(?:```|\Z)|~~~.*?(?:~~~|\Z)|`[^`\n]*`|<!--.*?-->", re.S)
PEOPLE = {"OWNER", "MEMBER", "COLLABORATOR"}  # whose comments count: not a drive-by on a public repo
FROM = {"Todo", "In progress", None}  # a Waiting on line keeps In review, Blocked and Done


def waiting_on(body):
    """The text after `Waiting on` when the comment says the work waits on a person, else None.
    Code, inline code and quoted (`>`) lines don't count."""
    text = QUOTED.sub("", body or "")
    for found in WAITING_LINE.finditer(text):
        rest = found.group(1)
        if not NOT_WAITING.match(rest):
            return rest.strip(" :*_–-")
    return None


def decide(body, status, assigned):
    """(Status to set or None, logins to assign) for a new comment."""
    line = waiting_on(body)
    if line is not None:
        return (WAITING if status in FROM else None), MENTION.findall(line)
    if status == WAITING:
        return ("In progress" if assigned else "Todo"), []
    return None, []


def main(path):
    event = json.load(open(path))
    issue, comment = event["issue"], event["comment"]
    if (event.get("action") != "created" or "pull_request" in issue or issue.get("state") != "open"
            or (comment.get("user") or {}).get("type") == "Bot"
            or comment.get("author_association") not in PEOPLE):
        print("Nothing to change: not a new comment on an open issue by someone in the org")
        return 0
    repo, number = event["repository"]["full_name"], issue["number"]
    board = find_board_fields("Akenon Studio")
    loaded = load_issue(repo, number)
    item, status = board_item(board, loaded)
    status_to, people = decide(comment.get("body"), status, bool(loaded["assignedActors"]["nodes"]))
    if people:  # GitHub drops anyone who can't be assigned here
        gh(f"repos/{repo}/issues/{number}/assignees", "-X", "POST", body={"assignees": people})
        print(f"#{number}: assigned {', '.join(people)}")
    if status_to:
        set_field(board, item, "Status", status_to)
        print(f"#{number}: {status} -> {status_to}")
    else:
        print(f"#{number}: stays {status}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1]))

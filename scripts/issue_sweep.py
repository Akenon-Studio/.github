#!/usr/bin/env python3
"""Re-check issues on a schedule, for changes that send no event every repo's caller listens to
(design 6.8): a board Status set to Blocked, a parent or "blocked by" link added or removed, a
parent closed while its sub-issues are open. Runs the issue_fields.py checks on every open issue in
the managed repos and syncs only those whose `needs-fields` label or comment is out of date, then
reopens any parent closed in the last few hours that still has open sub-issues.

Usage: scripts/issue_sweep.py
In Actions it runs from the issue-sweep workflow with the org GitHub App token in GH_TOKEN.
"""

import datetime
import json

from board import find_board_fields, graphql, spec
from issue_fields import (ISSUE_FIELDS, LABEL, MARKER, all_problems, load_forms, open_sub_issues,
                          problems_text, status_on, sync)
from rules import ORG, managed_repos

CLOSED_WINDOW = datetime.timedelta(hours=6)  # covers late or skipped scheduled runs


def search(query):
    """Every issue matching a search, with what the checks read and the issue's comments."""
    nodes, after = [], None
    while True:
        data = graphql(f"""query($q: String!, $after: String) {{
            search(type: ISSUE, query: $q, first: 50, after: $after) {{
              pageInfo {{ hasNextPage endCursor }}
              nodes {{ ... on Issue {{ {ISSUE_FIELDS}
                comments(first: 100) {{ nodes {{ body }} }} }} }} }} }}""", q=query, after=after)
        page = data["search"]
        nodes += [n for n in page["nodes"] if n]
        if not page["pageInfo"]["hasNextPage"]:
            return nodes
        after = page["pageInfo"]["endCursor"]


def out_of_date(issue, forms, board_id):
    """True if the issue's label or comment doesn't match what the checks find now."""
    problems = all_problems(issue, forms, status_on(board_id, issue))
    labelled = LABEL in {l["name"] for l in issue["labels"]["nodes"]}
    ours = [c["body"] for c in issue["comments"]["nodes"] if c["body"].startswith(MARKER)]
    if bool(problems) != labelled:
        return True
    return bool(problems) and (not ours or ours[0] != problems_text(problems))


def in_managed_repo(issue, managed):
    owner, name = issue["repository"]["nameWithOwner"].split("/")
    return owner.lower() == ORG and name in managed


def main():
    forms, managed = load_forms(), set(managed_repos())
    board = find_board_fields(spec()["title"])
    results = []
    for issue in search(f"org:{ORG} is:issue is:open"):
        if in_managed_repo(issue, managed) and out_of_date(issue, forms, board["id"]):
            results.append(sync(issue["repository"]["nameWithOwner"], issue["number"], forms, board))
    since = (datetime.datetime.now(datetime.timezone.utc) - CLOSED_WINDOW).strftime("%Y-%m-%dT%H:%M:%SZ")
    for issue in search(f"org:{ORG} is:issue is:closed closed:>={since}"):
        if in_managed_repo(issue, managed) and open_sub_issues(issue["subIssues"]["nodes"]):
            results.append(sync(issue["repository"]["nameWithOwner"], issue["number"], forms, board))
    print(json.dumps(results, indent=2) if results else "Every issue is up to date.")


if __name__ == "__main__":
    main()

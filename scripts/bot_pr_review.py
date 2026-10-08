#!/usr/bin/env python3
"""Ask the automation owners team to review every open PR a bot or app opened (design 6.8): a PR
nobody is asked to review notifies no one. Runs on the issue-sweep schedule, as the org app, so it
needs no secrets in each repo's callers and also covers PRs whose own workflow runs get no secrets
(Dependabot's). A PR is asked once: if the team was ever requested (the timeline shows it), a
later removal or a finished review is left alone.

A CODEOWNERS entry was not used: it cannot tell a bot's PR from a person's, so it would also ask
the team to review people's changes to the same files.

Usage: scripts/bot_pr_review.py [--dry-run]
"""

import sys

from board import graphql
from rules import AUTOMATION_OWNERS, ORG, gh, managed_repos

PR_FIELDS = """number author { __typename login } repository { nameWithOwner }
  timelineItems(itemTypes: [REVIEW_REQUESTED_EVENT], first: 100) { nodes {
    ... on ReviewRequestedEvent { requestedReviewer { __typename ... on Team { slug } } } } }"""


def open_prs():
    """Every open PR in the org, with what needs_review() reads."""
    nodes, after = [], None
    while True:
        data = graphql(f"""query($q: String!, $after: String) {{
            search(type: ISSUE, query: $q, first: 50, after: $after) {{
              pageInfo {{ hasNextPage endCursor }}
              nodes {{ ... on PullRequest {{ {PR_FIELDS} }} }} }} }}""",
                       q=f"org:{ORG} is:pr is:open", after=after)
        nodes += [n for n in data["search"]["nodes"] if n]
        if not data["search"]["pageInfo"]["hasNextPage"]:
            return nodes
        after = data["search"]["pageInfo"]["endCursor"]


def needs_review(pr, managed, team=AUTOMATION_OWNERS):
    """True for a bot's open PR in a managed repo that the team was never asked to review."""
    owner, name = pr["repository"]["nameWithOwner"].split("/")
    if owner.lower() != ORG or name not in managed:
        return False
    if (pr["author"] or {}).get("__typename") != "Bot":
        return False
    asked = [(e.get("requestedReviewer") or {}).get("slug") for e in pr["timelineItems"]["nodes"]]
    return team not in asked


def main():
    dry_run = "--dry-run" in sys.argv[1:]
    managed = set(managed_repos())
    todo = [pr for pr in open_prs() if needs_review(pr, managed)]
    for pr in todo:
        repo, n = pr["repository"]["nameWithOwner"], pr["number"]
        if not dry_run:
            gh(f"repos/{repo}/pulls/{n}/requested_reviewers", "-X", "POST",
               body={"team_reviewers": [AUTOMATION_OWNERS]})
        print(f"{repo}#{n} ({pr['author']['login']}): {'would ask' if dry_run else 'asked'} "
              f"{AUTOMATION_OWNERS} to review")
    if not todo:
        print("Every bot PR has had a review requested.")


if __name__ == "__main__":
    main()

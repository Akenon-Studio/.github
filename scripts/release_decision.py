#!/usr/bin/env python3
"""The `release-decision` check (design 6.3, 6.6, 7.12): a Release PR merges only once a partner's own
comment on it records the decision to cut the release, made after the PR's latest commit.

A Release PR is one the release app (`akenon-studio-release`, rulesets/bots.json) opened, one on a
`release-please--` branch, or one labelled `autorelease: pending`, now or ever (taking the label off
doesn't make it an ordinary PR). It passes when a comment on it is from a person in
rulesets/teams.json (rules.org_people(); a bot's never counts), has no agent marker (`<!-- agent -->`,
design 6.4) and was made at or after the head commit's commit date. Every other PR passes.

Usage: scripts/release_decision.py "$GITHUB_EVENT_PATH"
(GITHUB_EVENT_NAME, GITHUB_REPOSITORY, GITHUB_REF_NAME and GITHUB_SHA from Actions; gh reads GH_TOKEN)
In Actions it is the `decision` job of the reusable release-decision workflow. By event:
- pull_request: judges the event's PR.
- workflow_dispatch: judges the open PR whose head is this run's branch and commit, in this repo; it
  takes no input, so a run can't be pointed at another PR's comments. No such PR fails it.
- issue_comment: on an open Release PR in this repo, starts the caller (`release-decision-caller.yml`)
  on the PR's branch, as the merge lane restarts itself, so the check's required name lands on the
  PR's head; the comment event's own run reports on main, under another name.
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime

from rules import bot_login, gh, org_people

RELEASE_APP = "akenon-studio-release"
RELEASE_BRANCH = "release-please--"
RELEASE_LABEL = "autorelease: pending"
AGENT = re.compile(r"<!--\s*agent\s*-->", re.IGNORECASE)  # what an agent's comment carries (design 6.4)
CALLER = "release-decision-caller.yml"

PR_FIELDS = """
  number url state isCrossRepository headRefName headRefOid
  author { __typename login }
  labels(first: 100) { nodes { name } }
  timelineItems(itemTypes: [LABELED_EVENT], first: 100) { nodes { ... on LabeledEvent { label { name } } } }
  commits(last: 1) { nodes { commit { oid committedDate } } }
  comments(last: 100) { nodes { author { __typename login } body createdAt url } }
"""


def graphql(query, **variables):
    res = gh("graphql", body={"query": query, "variables": variables})
    if res.get("errors"):
        sys.exit(f"GraphQL error: {res['errors']}")
    return res["data"]


def when(stamp):
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def is_release_pr(pr):
    """True for a Release PR: the release app's, on a release-please branch, or labelled
    `autorelease: pending` now or at any time."""
    author = pr.get("author") or {}
    if author.get("__typename") == "Bot" and bot_login(author.get("login")) == RELEASE_APP:
        return True
    if (pr.get("headRefName") or "").startswith(RELEASE_BRANCH):
        return True
    labels = {l["name"] for l in pr["labels"]["nodes"]}
    labels |= {(e.get("label") or {}).get("name") for e in pr["timelineItems"]["nodes"]}
    return RELEASE_LABEL in labels


def decision(pr, people):
    """The comment that records the decision to cut this release, or None. `people` are the
    partners' lowercase logins."""
    commits = pr["commits"]["nodes"]
    if not commits:
        return None
    head = when(commits[0]["commit"]["committedDate"])
    for comment in reversed(pr["comments"]["nodes"]):
        author = comment.get("author") or {}
        if (author.get("__typename") == "User" and (author.get("login") or "").lower() in people
                and not AGENT.search(comment.get("body") or "") and when(comment["createdAt"]) >= head):
            return comment
    return None


def problems(pr, people):
    """What stops the PR, as sentences. Empty means it passes."""
    if not is_release_pr(pr):
        return []
    if decision(pr, people):
        return []
    return [f"Release PR #{pr['number']} has no decision to cut the release: a partner (a person in "
            ".github's rulesets/teams.json) comments on it, in their own words and without the agent "
            "marker, after its latest commit. Every push needs a new comment, since it changes what "
            "would ship (design 6.3, 6.6; 7.12 for a mandated session's merge, whose `Decision:` "
            "line cites that comment). The comment runs this check again."]


def pr_by_number(repo, number):
    owner, name = repo.split("/")
    return graphql(f"""query($o: String!, $n: String!, $i: Int!) {{ repository(owner: $o, name: $n) {{
        pullRequest(number: $i) {{ {PR_FIELDS} }} }} }}""", o=owner, n=name, i=number)["repository"]["pullRequest"]


def pr_by_head(repo, branch, sha):
    """The open PR in this repo whose head is `branch` at `sha`, or None."""
    owner, name = repo.split("/")
    prs = graphql(f"""query($o: String!, $n: String!, $b: String!) {{ repository(owner: $o, name: $n) {{
        pullRequests(headRefName: $b, states: OPEN, first: 20) {{ nodes {{ {PR_FIELDS} }} }} }} }}""",
                  o=owner, n=name, b=branch)["repository"]["pullRequests"]["nodes"]
    return next((p for p in prs if not p["isCrossRepository"] and p["headRefOid"] == sha), None)


def judge(pr):
    found = problems(pr, org_people())
    for p in found:
        print(f"::error::{p}")
    if found:
        sys.exit(1)
    if is_release_pr(pr):
        print(f"OK: the decision to cut this release is {decision(pr, org_people())['url']}")
    else:
        print(f"OK: #{pr['number']} is not a Release PR")


def rerun(repo, event):
    """On a comment on an open Release PR in this repo, start the check on the PR's branch."""
    issue = event.get("issue") or {}
    if "pull_request" not in issue or issue.get("state") != "open":
        print("Not an open PR: nothing to re-run")
        return
    pr = pr_by_number(repo, issue["number"])
    if pr["state"] != "OPEN" or pr["isCrossRepository"] or not is_release_pr(pr):
        print(f"#{pr['number']} is not an open Release PR in this repo: nothing to re-run")
        return
    subprocess.run([os.environ.get("GH", "gh"), "workflow", "run", CALLER, "-R", repo,
                    "--ref", pr["headRefName"]], check=True)
    print(f"Started {CALLER} on {pr['headRefName']} for #{pr['number']}")


def main():
    event_name, repo = os.environ["GITHUB_EVENT_NAME"], os.environ["GITHUB_REPOSITORY"]
    with open(sys.argv[1]) as f:
        event = json.load(f)
    if event_name in ("pull_request", "pull_request_target"):
        judge(pr_by_number(repo, event["pull_request"]["number"]))
    elif event_name == "workflow_dispatch":
        branch, sha = os.environ["GITHUB_REF_NAME"], os.environ["GITHUB_SHA"]
        pr = pr_by_head(repo, branch, sha)
        if not pr:
            sys.exit(f"::error::No open PR in {repo} has {branch} at {sha} as its head: nothing to judge "
                     "(the branch may have moved on; its new commit runs this check itself)")
        judge(pr)
    elif event_name == "issue_comment":
        rerun(repo, event)
    else:
        sys.exit(f"::error::release-decision doesn't run on {event_name}")


if __name__ == "__main__":
    main()

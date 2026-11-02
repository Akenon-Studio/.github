#!/usr/bin/env python3
"""The `release-decision` check (design 6.3, 6.6, 7.12): a Release PR merges only once a partner's own
comment on it records the decision to cut the release, made after the PR's head commit became its head.

A Release PR is one the release app (`akenon-studio-release`, rulesets/bots.json) opened, one on a
`release-please--` branch, or one labelled `autorelease: pending`, now or ever (taking the label off
doesn't make it an ordinary PR). It passes when a comment on it is from a person in
rulesets/teams.json (rules.org_people(); a bot's never counts), has no agent marker (`<!-- agent -->`,
design 6.4) and is at least as new as the latest push to the PR's head branch that made the head
commit its head, as GitHub's repository activity API records it on the server. No commit date is read
(both come from the pushing machine's clock), and a head with no such push, such as a fork's, fails.
Every other PR passes.

Usage: scripts/release_decision.py "$GITHUB_EVENT_PATH"
(GITHUB_EVENT_NAME and GITHUB_REPOSITORY from Actions; gh reads GH_TOKEN)
In Actions it is the `decision` job of the reusable release-decision workflow. By event:
- pull_request_target (or pull_request): judges the event's PR. pull_request_target runs main's
  workflow, so a push to the PR's branch can't change the check that judges it.
- issue_comment: on an open Release PR, re-runs the check's latest run on the PR's head (waiting for
  it to finish first), so the required name is reported again on the head with the comment counted.
  The comment's own run reports on main, under another name. It finds that run by the workflow's
  name, so it works from a repo's caller or from an org ruleset's required workflow alike.
"""

import json
import os
import re
import sys
import time
from datetime import datetime

from rules import bot_login, gh, org_people

RELEASE_APP = "akenon-studio-release"
RELEASE_BRANCH = "release-please--"
RELEASE_LABEL = "autorelease: pending"
AGENT = re.compile(r"<!--\s*agent\s*-->", re.IGNORECASE)  # what an agent's comment carries (design 6.4)
# The check's runs: a repo's caller workflow, or the reusable workflow run as a required workflow
WORKFLOWS = {"release-decision-caller", "release-decision"}
PR_EVENTS = ("pull_request_target", "pull_request")
PUSHES = {"push", "force_push", "branch_creation"}  # activity types that move a branch's head
WAIT_SECONDS, POLLS = 10, 18

PR_FIELDS = """
  number url state isCrossRepository headRefName headRefOid
  author { __typename login }
  labels(first: 100) { nodes { name } }
  timelineItems(itemTypes: [LABELED_EVENT], first: 100) { nodes { ... on LabeledEvent { label { name } } } }
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


def head_pushed_at(activity, branch, sha):
    """When `sha` last became `branch`'s head, from the repository activity API's entries, or None."""
    times = [a["timestamp"] for a in activity
             if a.get("activity_type") in PUSHES and a.get("after") == sha
             and a.get("ref") == f"refs/heads/{branch}"]
    return max(times, key=when) if times else None


def decision(pr, people, pushed):
    """The newest comment that records the decision to cut this release, or None. `people` are the
    partners' lowercase logins; `pushed` is when the head commit became the head."""
    if not pushed:
        return None
    for comment in reversed(pr["comments"]["nodes"]):
        author = comment.get("author") or {}
        if (author.get("__typename") == "User" and (author.get("login") or "").lower() in people
                and not AGENT.search(comment.get("body") or "") and when(comment["createdAt"]) >= when(pushed)):
            return comment
    return None


def problems(pr, people, pushed):
    """What stops the PR, as sentences. Empty means it passes."""
    if not is_release_pr(pr):
        return []
    if not pushed:
        return [f"Release PR #{pr['number']}: GitHub records no push to {pr['headRefName']} that made "
                f"{pr['headRefOid'][:12]} its head (a fork's branch has none here), so no comment can be "
                "shown to postdate it (design 6.3). release-please opens its PR from a branch in this repo."]
    if decision(pr, people, pushed):
        return []
    return [f"Release PR #{pr['number']} has no decision to cut the release: a partner (a person in "
            ".github's rulesets/teams.json) comments on it, without the agent marker, after its head "
            f"was pushed ({pushed}). Every push needs a new comment, since it changes what would ship "
            "(design 6.3, 6.6; 7.12 for a mandated session's merge, whose `Decision:` line cites that "
            "comment). The comment runs this check again."]


def pr_by_number(repo, number):
    owner, name = repo.split("/")
    return graphql(f"""query($o: String!, $n: String!, $i: Int!) {{ repository(owner: $o, name: $n) {{
        pullRequest(number: $i) {{ {PR_FIELDS} }} }} }}""", o=owner, n=name, i=number)["repository"]["pullRequest"]


def pushed_at(repo, pr):
    """When the PR's head commit became its head, or None (a fork's branch has no activity here)."""
    if pr["isCrossRepository"]:
        return None
    activity = gh(f"repos/{repo}/activity?ref=refs/heads/{pr['headRefName']}&per_page=100") or []
    return head_pushed_at(activity, pr["headRefName"], pr["headRefOid"])


def judge(repo, number):
    pr = pr_by_number(repo, number)
    if not is_release_pr(pr):
        print(f"OK: #{pr['number']} is not a Release PR")
        return
    pushed = pushed_at(repo, pr)
    found = problems(pr, org_people(), pushed)
    for p in found:
        print(f"::error::{p}")
    if found:
        sys.exit(1)
    print(f"OK: the decision to cut this release is {decision(pr, org_people(), pushed)['url']}")


def latest_check_run(repo, sha):
    """The newest run of this check on `sha` from a PR event, or None."""
    runs = gh(f"repos/{repo}/actions/runs?head_sha={sha}&per_page=100")["workflow_runs"]
    runs = [r for r in runs if r.get("name") in WORKFLOWS and r.get("event") in PR_EVENTS]
    return max(runs, key=lambda r: r["id"]) if runs else None


def rerun(repo, event):
    """On a comment on an open Release PR, run the check again on the PR's head."""
    issue = event.get("issue") or {}
    if "pull_request" not in issue or issue.get("state") != "open":
        print("Not an open PR: nothing to re-run")
        return
    pr = pr_by_number(repo, issue["number"])
    if pr["state"] != "OPEN" or not is_release_pr(pr):
        print(f"#{pr['number']} is not an open Release PR: nothing to re-run")
        return
    run = latest_check_run(repo, pr["headRefOid"])
    if not run:
        print(f"::warning::No release-decision run on #{pr['number']}'s head to re-run; a push or a "
              "label change starts one")
        return
    # a run still going may have read the comments before this one: let it finish, then re-run it
    for _ in range(POLLS):
        if run["status"] == "completed":
            break
        time.sleep(WAIT_SECONDS)
        run = gh(f"repos/{repo}/actions/runs/{run['id']}")
    else:
        sys.exit(f"::error::Run {run['html_url']} hasn't finished; re-run it once it has")
    gh(f"repos/{repo}/actions/runs/{run['id']}/rerun", "-X", "POST")
    print(f"Re-ran {run['html_url']} for #{pr['number']}")


def main():
    event_name, repo = os.environ["GITHUB_EVENT_NAME"], os.environ["GITHUB_REPOSITORY"]
    with open(sys.argv[1]) as f:
        event = json.load(f)
    if event_name in PR_EVENTS:
        judge(repo, event["pull_request"]["number"])
    elif event_name == "issue_comment":
        rerun(repo, event)
    else:
        sys.exit(f"::error::release-decision doesn't run on {event_name}")


if __name__ == "__main__":
    main()

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

Usage: scripts/release_decision.py judge|rerun "$GITHUB_EVENT_PATH"
(GITHUB_REPOSITORY from Actions; gh reads GH_TOKEN)
- judge: the `decision` job of release-decision.yml, which the org ruleset "Release decision"
  (rulesets/org-rulesets.json) runs from `.github` main on every PR to main in the repos it names, so
  no repo, branch or PR can change it. It judges the event's PR.
- rerun: the `rerun` job of release-decision-rerun.yml, which each of those repos calls from main on
  a comment (issue_comment) and a label change (pull_request_target); both run the default branch's
  workflow, never a release branch's. A ruleset's workflow runs only on pull_request events, and only
  on opened, synchronize and reopened, so on an open Release PR this re-runs the check's latest run on
  the PR's head (waiting for it to finish first), and the comment is counted under the required check.
  A workflow token may re-run a ruleset's run (tried on .github, 2026-11-02, .github#248). In a repo
  the ruleset names, GitHub reports its run with the source workflow's name and path, as here (seen
  for "Action pins" in peras and platform, which have no action-pins.yml of their own).
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

from rules import bot_login, gh, org_people

RELEASE_APP = "akenon-studio-release"
RELEASE_BRANCH = "release-please--"
RELEASE_LABEL = "autorelease: pending"
AGENT = re.compile(r"<!--\s*agent\s*-->", re.IGNORECASE)  # what an agent's comment carries (design 6.4)
# The ruleset's runs of the check: its workflow's name and path, on a pull_request event
WORKFLOW, WORKFLOW_PATH, PR_EVENT = "release-decision", ".github/workflows/release-decision.yml", "pull_request"
PUSHES = {"push", "force_push", "branch_creation"}  # activity types that move a branch's head
WAIT_SECONDS, POLLS = 10, 18
RERUN_DAYS = 30  # GitHub re-runs no run older than this

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


def latest_check_runs(repo, sha):
    """The newest run of this check on `sha` from a PR event, one per workflow (a repo's own workflow
    of the same name and path is re-run too, but can't stand in for the ruleset's)."""
    runs = gh(f"repos/{repo}/actions/runs?head_sha={sha}&event={PR_EVENT}&per_page=100")["workflow_runs"]
    latest = {}
    for r in runs:
        if r.get("name") == WORKFLOW and r.get("path") == WORKFLOW_PATH and r.get("event") == PR_EVENT:
            if r["workflow_id"] not in latest or r["id"] > latest[r["workflow_id"]]["id"]:
                latest[r["workflow_id"]] = r
    return sorted(latest.values(), key=lambda r: r["id"])


def rerun(repo, event):
    """On a comment on, or a label change of, an open Release PR, run the check again on its head."""
    if "issue" in event:
        issue = event["issue"]
        if "pull_request" not in issue or issue.get("state") != "open":
            print("Not an open PR: nothing to re-run")
            return
        number = issue["number"]
    else:
        number = event["pull_request"]["number"]
    pr = pr_by_number(repo, number)
    if pr["state"] != "OPEN" or not is_release_pr(pr):
        print(f"#{pr['number']} is not an open Release PR: nothing to re-run")
        return
    runs = latest_check_runs(repo, pr["headRefOid"])
    if not runs:
        print(f"::warning::No release-decision run on #{pr['number']}'s head to re-run; a push starts one")
        return
    for run in runs:
        rerun_once_finished(repo, run, pr["number"])


def rerun_once_finished(repo, run, number):
    """Re-run `run` once it has finished. A run still going may have read the comments before this
    one, and so may a re-run another comment started just before this one (GitHub then refuses this
    re-run): either way, wait for it to finish and re-run it again."""
    error = "it hadn't finished"
    for poll in range(POLLS + 1):
        if poll:
            time.sleep(WAIT_SECONDS)
            run = gh(f"repos/{repo}/actions/runs/{run['id']}")
        if run["status"] != "completed":
            continue
        if datetime.now(timezone.utc) - when(run["created_at"]) > timedelta(days=RERUN_DAYS):
            sys.exit(f"::error::Run {run['html_url']} is over {RERUN_DAYS} days old, and GitHub re-runs "
                     f"no run that old: close and reopen #{number}, which runs the check again")
        try:
            gh(f"repos/{repo}/actions/runs/{run['id']}/rerun", "-X", "POST")
        except SystemExit as e:
            error = e.code
            continue
        print(f"Re-ran {run['html_url']} for #{number}")
        return
    sys.exit(f"::error::Couldn't re-run {run['html_url']} ({error}); re-run it once it has finished")

MODES = {"judge": lambda repo, event: judge(repo, event["pull_request"]["number"]), "rerun": rerun}


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in MODES:
        sys.exit(__doc__)
    with open(sys.argv[2]) as f:
        event = json.load(f)
    MODES[sys.argv[1]](os.environ["GITHUB_REPOSITORY"], event)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""The merge lane (design 6.2): merge a repo's approved, green PRs one at a time.

An author labels a PR `ready-to-merge` once it is approved and green, and moves on. This takes the
labelled PRs oldest label first and, for each: brings `main` in if the branch is behind (once, now
that it is next), waits for its required checks, and squash-merges it. A PR that can't merge
(conflict, a failed or missing required check, a missing approval or an unresolved conversation,
a draft) gets a comment saying why and loses the label, and the lane goes on to the next. It never
bypasses the rules: GitHub refuses a merge they don't allow.

It logs in as the Akenon Studio Merge Lane app: a branch update made with the workflow's own token
would start no workflows, so the checks would never run on it. One run at a time per repo (the
workflow's concurrency group); every run works through the whole queue, so a run that GitHub drops
while another is going loses nothing. A run stops starting work at its deadline and exits with
MORE_LEFT, and the workflow starts a fresh run for the rest.

Usage: scripts/merge_lane.py   (in Actions, as the reusable merge-lane workflow; reads GITHUB_TOKEN
(the app's), GITHUB_REPOSITORY)
"""

import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request

LABEL = "ready-to-merge"
POLL = 15                 # seconds between looks at a PR
CHECKS_TIMEOUT = 40 * 60  # the longest one PR may stay at the front before it leaves the lane
NEVER_RAN = 10 * 60       # a required check the lane has seen with no run for this long never will
BLOCKED_LOOKS = 4         # green but BLOCKED this many looks in a row (GitHub lags a few seconds)
MAX_UPDATES = 3           # main moved under it this many times: someone else keeps merging
MAX_REFUSALS = 3          # GitHub refused the merge this many times in a row: it won't change
DEADLINE = 90 * 60        # a run starts no new wait after this (the job's timeout is 120 minutes)
MORE_LEFT = 3             # exit code: PRs are still labelled; start another run
PASSING = {"SUCCESS", "NEUTRAL", "SKIPPED"}
MERGEABLE = {"CLEAN", "UNSTABLE", "HAS_HOOKS"}  # UNSTABLE: only a check that isn't required failed
TRANSIENT = {500, 502, 503, 504}

QUEUE = """query($owner: String!, $name: String!, $label: String!) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, labels: [$label], first: 100) {
      nodes { number
        timelineItems(itemTypes: [LABELED_EVENT], last: 50) {
          nodes { ... on LabeledEvent { createdAt label { name } } } } } } } }"""

PR = """query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      number state isDraft mergeable mergeStateStatus reviewDecision headRefOid
      author { login }
      labels(first: 100) { nodes { name } }
      reviewThreads(first: 100) { nodes { isResolved } }
      comments(last: 20) { nodes { body } }
      commits(last: 1) { nodes { commit { oid
        statusCheckRollup { contexts(first: 100) {
          pageInfo { hasNextPage }
          nodes {
            __typename
            ... on CheckRun { name status conclusion startedAt isRequired(pullRequestNumber: $number) }
            ... on StatusContext { context state createdAt isRequired(pullRequestNumber: $number) }
          } } } } } } } } }"""


class GitHub:
    def __init__(self, token, repo):
        self.token, self.repo = token, repo
        self.owner, self.name = repo.split("/")

    def call(self, path, method="GET", body=None, tries=4):
        """One REST or GraphQL call; a 5xx or a network error is retried with backoff."""
        for attempt in range(tries):
            req = urllib.request.Request(f"https://api.github.com/{path}", method=method,
                                         data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Authorization": f"Bearer {self.token}",
                                                  "Accept": "application/vnd.github+json",
                                                  "Content-Type": "application/json"})
            try:
                # nosemgrep: dynamic-urllib-use-detected -- always https://api.github.com/ plus a fixed path
                with urllib.request.urlopen(req, timeout=30) as res:
                    raw = res.read().decode()
                return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:
                if e.code not in TRANSIENT or attempt == tries - 1:
                    raise
            except (OSError, http.client.HTTPException):  # URLError, timeouts, resets, short reads
                if attempt == tries - 1:
                    raise
            time.sleep(5 * 2 ** attempt)

    def graphql(self, query, **variables):
        out = self.call("graphql", "POST", {"query": query, "variables": {
            "owner": self.owner, "name": self.name, **variables}})
        if out.get("errors"):
            raise RuntimeError(f"GraphQL: {out['errors']}")
        return out["data"]["repository"]

    def required_checks(self):
        """The required status checks on main, from the repo's rules."""
        rules = self.call(f"repos/{self.repo}/rules/branches/main") or []
        return {c["context"] for r in rules if r["type"] == "required_status_checks"
                for c in r["parameters"]["required_status_checks"]}


def queue(repo_data):
    """Labelled PR numbers, oldest label first (the latest time each got the label)."""
    when = {}
    for pr in repo_data["pullRequests"]["nodes"]:
        times = [e["createdAt"] for e in pr["timelineItems"]["nodes"]
                 if (e.get("label") or {}).get("name") == LABEL]
        when[pr["number"]] = max(times) if times else ""
    return sorted(when, key=lambda n: (when[n], n))


def check_states(pr, required):
    """{required check: PASS, FAIL or PENDING}; one with no run at all is MISSING."""
    commit = pr["commits"]["nodes"][0]["commit"]
    contexts = ((commit.get("statusCheckRollup") or {}).get("contexts") or {}).get("nodes") or []
    latest = {}  # {name: (started, state)}: a re-run leaves the old run in the list; the newest counts
    for c in contexts:
        if c["__typename"] == "CheckRun":
            name, started = c["name"], c.get("startedAt") or ""
            state = ("PENDING" if c["status"] != "COMPLETED"
                     else "PASS" if c["conclusion"] in PASSING else "FAIL")
        else:
            name, started = c["context"], c.get("createdAt") or ""
            state = {"SUCCESS": "PASS", "PENDING": "PENDING", "EXPECTED": "PENDING"}.get(c["state"], "FAIL")
        if (name in required or c.get("isRequired")) and started >= latest.get(name, ("",))[0]:
            latest[name] = (started, state)
    states = {name: state for name, (_, state) in latest.items()}
    for name in required:
        states.setdefault(name, "MISSING")
    return states


def too_many_checks(pr):
    commit = pr["commits"]["nodes"][0]["commit"]
    return (((commit.get("statusCheckRollup") or {}).get("contexts") or {}).get("pageInfo")
            or {}).get("hasNextPage", False)


def decide(pr, required, missing_for):
    """What to do with the PR at the front of the lane: (action, reason).
    action: merge, update (bring main in), wait, blocked (green, but the rules block it: dropped
    once it stays so), drop (leave the lane), gone (no longer in it).
    `missing_for`: seconds the lane has seen a required check with no run on this commit."""
    if pr["state"] != "OPEN" or LABEL not in {l["name"] for l in pr["labels"]["nodes"]}:
        return "gone", "no longer open or labelled"
    if pr["isDraft"]:
        return "drop", "it is a draft"
    if pr["mergeable"] == "CONFLICTING" or pr["mergeStateStatus"] == "DIRTY":
        return "drop", "it conflicts with main: merge main in and resolve, then label it again"
    if too_many_checks(pr):
        return "drop", "it has over 100 checks, more than the lane reads: merge it by hand"
    # Before bringing main in: a PR that can't merge anyway isn't worth a CI run.
    states = check_states(pr, required)
    failed = sorted(n for n, s in states.items() if s == "FAIL")
    if failed:
        return "drop", "required checks failed: " + ", ".join(failed)
    waiting_on = people_needed(pr)
    if waiting_on:
        return "blocked", "the rules block it: " + waiting_on + ". Fix that, then label it again"
    if pr["mergeStateStatus"] == "BEHIND":
        return "update", "behind main"
    if pr["mergeable"] == "UNKNOWN" or pr["mergeStateStatus"] == "UNKNOWN":
        return "wait", "GitHub is still working out whether it can merge"
    missing = sorted(n for n, s in states.items() if s == "MISSING")
    if missing and missing_for > NEVER_RAN:
        return "drop", ("required checks never ran on its last commit: " + ", ".join(missing)
                        + ". Push a commit (or edit the description, for pr-title) to run them")
    if missing or "PENDING" in states.values():
        return "wait", "required checks still running"
    if pr["mergeStateStatus"] in MERGEABLE:
        return "merge", "approved and green"
    return "blocked", f"the rules block it (GitHub says {pr['mergeStateStatus']}). Fix that, then label it again"


def people_needed(pr):
    """What a person still has to do before it can merge: unresolved conversations, an approval."""
    unresolved = sum(not t["isResolved"] for t in pr["reviewThreads"]["nodes"])
    why = [f"{unresolved} unresolved conversation(s)"] if unresolved else []
    if pr["reviewDecision"] == "REVIEW_REQUIRED":
        why.append("it needs an approval")
    elif pr["reviewDecision"] == "CHANGES_REQUESTED":
        why.append("changes were requested")
    return ", ".join(why)


def drop(gh, pr, reason):
    """Say why, then take the label off (so a failed comment never leaves a silent drop). The same
    reason isn't said twice: a run that commented but couldn't take the label off says it once."""
    who = (pr.get("author") or {}).get("login")
    body = (f"<!-- merge-lane -->\n{'@' + who + ' ' if who else ''}Taken out of the merge lane: {reason}. "
            f"Label it `{LABEL}` again when it's ready (design 6.2).")
    said = [c["body"] for c in (pr.get("comments") or {}).get("nodes", [])]
    if not said or said[-1] != body:
        gh.call(f"repos/{gh.repo}/issues/{pr['number']}/comments", "POST", {"body": body})
    try:
        gh.call(f"repos/{gh.repo}/issues/{pr['number']}/labels/{LABEL}", "DELETE")
    except urllib.error.HTTPError as e:
        if e.code != 404:  # already taken off
            raise
    print(f"#{pr['number']}: left the lane ({reason})")


def github_message(error):
    """GitHub's own message from an error response, for the author."""
    try:
        return json.loads(error.read().decode()).get("message", "") or str(error.code)
    except (ValueError, AttributeError, OSError):
        return str(error.code)


def run_one(gh, number, required, deadline):
    """Take one PR through the lane. Returns "done" once it is merged, dropped or gone, or
    "paused" if the run's deadline came first (it stays labelled, at the front)."""
    started, missing_since, blocked_looks, updates, update_errors, refusals = time.time(), {}, 0, 0, 0, 0
    while True:
        pr = gh.graphql(PR, number=number)["pullRequest"]
        head = pr["headRefOid"]
        missing = "MISSING" in check_states(pr, required).values()
        if missing:
            missing_since.setdefault(head, time.time())
        action, reason = decide(pr, required, time.time() - missing_since[head] if missing else 0)
        blocked_looks = blocked_looks + 1 if action == "blocked" else 0
        if action == "gone":
            print(f"#{number}: {reason}")
            return "done"
        if action == "drop" or (action == "blocked" and blocked_looks >= BLOCKED_LOOKS):
            drop(gh, pr, reason)
            return "done"
        if action == "update":
            if updates >= MAX_UPDATES:
                drop(gh, pr, f"main moved under it {updates} times while it waited: someone is "
                             "merging outside the lane")
                return "done"
            try:
                gh.call(f"repos/{gh.repo}/pulls/{number}/update-branch", "PUT",
                        {"expected_head_sha": head})
                updates, update_errors = updates + 1, 0  # only failures in a row count
                print(f"#{number}: brought main in; waiting for its checks")
            except urllib.error.HTTPError as e:
                # 422: the head moved (an update already landing) or a conflict GitHub hasn't
                # flagged yet: look again, and give up only if it keeps failing
                update_errors += 1
                if update_errors >= 3:
                    drop(gh, pr, f"main couldn't be brought in (GitHub said {e.code}): merge it in by hand")
                    return "done"
                print(f"#{number}: bringing main in failed ({e.code}); looking again")
        if action == "merge":
            try:
                gh.call(f"repos/{gh.repo}/pulls/{number}/merge", "PUT",
                        {"merge_method": "squash", "sha": head})
                print(f"#{number}: merged")
                return "done"
            except urllib.error.HTTPError as e:
                if e.code not in (405, 409):  # 405: not mergeable right now; 409: head moved
                    raise
                said = github_message(e)
                refusals += 1
                if refusals >= MAX_REFUSALS:
                    drop(gh, pr, f"GitHub refused the merge {refusals} times ({e.code}: {said})")
                    return "done"
                print(f"#{number}: GitHub refused the merge ({e.code}: {said}); looking again")
        else:
            refusals = 0
        if time.time() - started > CHECKS_TIMEOUT:
            drop(gh, pr, f"it stayed at the front for over {CHECKS_TIMEOUT // 60} minutes without merging")
            return "done"
        if time.time() > deadline:
            print(f"#{number}: this run's time is up; a new run carries on")
            return "paused"
        time.sleep(POLL)


def main():
    gh = GitHub(os.environ["GITHUB_TOKEN"], os.environ["GITHUB_REPOSITORY"])
    required = gh.required_checks()
    deadline = time.time() + DEADLINE
    done, failed = set(), []
    while True:
        waiting = [n for n in queue(gh.graphql(QUEUE, label=LABEL)) if n not in done]
        if not waiting:
            print("The lane is empty." if not failed else f"Done; errors on {failed}: see above.")
            return 1 if failed else 0
        if time.time() > deadline:
            print(f"Time is up with {len(waiting)} PR(s) still labelled; starting a new run.")
            return MORE_LEFT
        print(f"Lane: {', '.join(f'#{n}' for n in waiting)}")
        number = waiting[0]
        try:
            if run_one(gh, number, required, deadline) == "paused":
                return MORE_LEFT
        except (OSError, http.client.HTTPException, ValueError, RuntimeError, KeyError, TypeError) as e:
            # One PR's error doesn't stop the others. It leaves the lane, saying so, rather than stay
            # labelled and hit the same error in every later run with no one told.
            print(f"::error::#{number}: {e!r}")
            failed.append(number)
            try:
                pr = gh.graphql(PR, number=number)["pullRequest"]
                drop(gh, pr, f"the lane hit an error ({e!r:.200}); see the merge-lane run's log")
            except Exception as again:  # noqa: BLE001 -- best effort; the log above has the cause
                print(f"::error::#{number}: couldn't say so on the PR either: {again!r}")
        done.add(number)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""The merge lane (design 6.2): merge a repo's approved, green PRs one at a time.

An author labels a PR `ready-to-merge` once it is approved and green, and moves on. This takes the
labelled PRs oldest label first and, for each: waits for any run of the AI review on its last
commit, brings `main` in if the branch is behind (once, now that it is next; after the review, which
the push would cancel), waits for its required checks and the review again, and squash-merges it.
The review stays advisory (design 6.4): it isn't a required check, the lane waits while its run is
queued or going, and a run that fails doesn't hold the PR; in a repo that runs the review, a commit
that shows no run yet is held up to REVIEW_GRACE (GitHub may show it after the checks), then merged
without it. One still going at CHECKS_TIMEOUT drops the PR, as a slow check does. The findings it
posts are review threads, which the rules want resolved, so an open one blocks the merge.
A PR that can't merge
(conflict, a failed or missing required check, a missing approval or an unresolved conversation,
a draft) gets a comment saying why and loses the label, and the lane goes on to the next. It never
bypasses the rules: GitHub refuses a merge they don't allow.

It logs in as the Akenon Studio Merge Lane app: a branch update made with the workflow's own token
would start no workflows, so the checks would never run on it. The app's token lasts an hour, so a
run stops at DEADLINE and exits with MORE_LEFT, and the workflow starts a fresh run (a fresh token)
for the rest. One run at a time per repo (the workflow's concurrency group); every run works
through the whole queue, so a run that GitHub drops while another is going loses nothing.

After a merge it closes the PR's closing issues itself (`Closes #n`): GitHub doesn't when an app
merges. It reads and closes them with a second token, limited to issues (and reading PRs) but for
every repo, since a PR may close an issue in another repo. It closes an issue only in a repo the
PR's author can write to (a bot author: only the PR's own repo); any it won't or can't close are
listed, and the run fails so a person closes them.

Only the PR's own state takes it out of the lane. An error that isn't about the PR (GitHub down
after the retries, a rate limit, an expired token) stops the run and fails it, leaving every label
on: GitHub tells whoever labelled it that the run failed, and the next label or run carries on.

Usage: scripts/merge_lane.py   (in Actions, as the reusable merge-lane workflow; reads GITHUB_TOKEN
(the app's, this repo), ISSUES_TOKEN (the app's, issues in every repo), GITHUB_REPOSITORY)
"""

import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request

from rules import acts_for

LABEL = "ready-to-merge"
POLL = 15                 # seconds between looks at a PR
CHECKS_TIMEOUT = 40 * 60  # the longest one PR may stay at the front before it leaves the lane
NEVER_RAN = 10 * 60       # a required check the lane has seen with no run for this long never will
BLOCKED_LOOKS = 4         # green but BLOCKED this many looks in a row (GitHub lags a few seconds)
MAX_UPDATES = 3           # main moved under it this many times: someone else keeps merging
MAX_REFUSALS = 3          # GitHub refused the merge this many times in a row: it won't change
DEADLINE = 45 * 60        # a run stops after this: the app's token expires at 60 minutes
UPDATE_LAG = 3 * 60       # after bringing main in, the longest GitHub may take to show the new commit
MORE_LEFT = 3             # exit code: PRs are still labelled; start another run
MORE_LEFT_UNCLOSED = 4    # exit code: both that, and issues to close by hand (start another run, fail)
REVIEW_CHECK = "ai-review / review"  # every repo's caller job is `ai-review` (ai-review.yml)
REVIEW_CALLER = ".github/workflows/ai-review-caller.yml"  # a repo with it on main has the review
REVIEW_GRACE = 3 * 60     # in such a repo, how long a new commit may show no review run before the lane
                          # takes it that none will start (GitHub queues it with the checks)
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
      number state isDraft mergeable mergeStateStatus reviewDecision headRefOid baseRefName
      author { __typename login } repository { isPrivate }
      labels(first: 100) { nodes { name } }
      reviewThreads(first: 100) { nodes { isResolved } }
      timelineItems(itemTypes: [ISSUE_COMMENT, LABELED_EVENT], last: 30) { nodes {
        __typename
        ... on IssueComment { body }
        ... on LabeledEvent { label { name } } } }
      commits(last: 1) { nodes { commit { oid
        statusCheckRollup { contexts(first: 100) {
          pageInfo { hasNextPage }
          nodes {
            __typename
            ... on CheckRun { name status conclusion startedAt completedAt isRequired(pullRequestNumber: $number) }
            ... on StatusContext { context state createdAt isRequired(pullRequestNumber: $number) }
          } } } } } } } } }"""


CLOSING = """query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      closingIssuesReferences(first: 50) {
        pageInfo { hasNextPage }
        nodes { number state repository { nameWithOwner } } } } } }"""


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

    def main_rules(self):
        """(required status checks, whether conversations must be resolved) on main, from the
        repo's rules."""
        rules = self.call(f"repos/{self.repo}/rules/branches/main") or []
        checks = {c["context"] for r in rules if r["type"] == "required_status_checks"
                  for c in r["parameters"]["required_status_checks"]}
        threads = any(r["type"] == "pull_request"
                      and r["parameters"].get("required_review_thread_resolution") for r in rules)
        return checks, threads

    def calls_review(self):
        """Whether this repo runs the AI review (design 6.4): its caller workflow is on main."""
        try:
            self.call(f"repos/{self.repo}/contents/{REVIEW_CALLER}")
            return True
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            return False


def queue(repo_data):
    """Labelled PR numbers, oldest label first (the latest time each got the label)."""
    when = {}
    for pr in repo_data["pullRequests"]["nodes"]:
        times = [e["createdAt"] for e in pr["timelineItems"]["nodes"]
                 if (e.get("label") or {}).get("name") == LABEL]
        # no labelling found (older than the events read): to the back, not the front
        when[pr["number"]] = max(times) if times else "~"
    return sorted(when, key=lambda n: (when[n], n))


def contexts_of(pr):
    commit = pr["commits"]["nodes"][0]["commit"]
    return ((commit.get("statusCheckRollup") or {}).get("contexts") or {}).get("nodes") or []


def review_state(pr):
    """The newest AI review run on the PR's last commit (design 6.4): "running" (queued or going),
    "done" (whatever its conclusion: the review is advisory), or None (no run yet)."""
    newest = None  # (started, still going)
    for c in contexts_of(pr):
        if c["__typename"] == "CheckRun" and c["name"] == REVIEW_CHECK:
            # as in check_states: a run still queued has no start time yet, and is the newest
            started = c.get("startedAt") or ("" if c["status"] == "COMPLETED" else "~")
            if newest is None or started >= newest[0]:
                newest = (started, c["status"] != "COMPLETED")
    return None if newest is None else "running" if newest[1] else "done"


def review_running(pr):
    return review_state(pr) == "running"


def check_states(pr, required):
    """{required check: PASS, FAIL or PENDING}; one with no run at all is MISSING."""
    contexts = contexts_of(pr)
    latest = {}  # {name: (started, state)}: a re-run leaves the old run in the list; the newest counts
    for c in contexts:
        if c["__typename"] == "CheckRun":
            # a run still queued has no start time yet, and is the newest
            name, started = c["name"], c.get("startedAt") or ("" if c["status"] == "COMPLETED" else "~")
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


def decide(pr, required, missing_for, threads_required=True, review_missing_for=None):
    """What to do with the PR at the front of the lane: (action, reason).
    action: merge, update (bring main in), wait, blocked (green, but the rules block it: dropped
    once it stays so), drop (leave the lane), gone (no longer in it).
    `missing_for`: seconds the lane has seen a required check with no run on this commit.
    `review_missing_for`: seconds it has seen no AI review run on this commit, in a repo that runs
    the review; None where it doesn't."""
    if pr["state"] != "OPEN" or LABEL not in {l["name"] for l in pr["labels"]["nodes"]}:
        return "gone", "no longer open or labelled"
    if pr["baseRefName"] != "main":
        return "drop", f"it targets `{pr['baseRefName']}`, and the lane merges only into `main`"
    if pr["isDraft"]:
        return "drop", "it is a draft"
    if not acts_for(pr.get("author"), (pr.get("repository") or {}).get("isPrivate", False)):
        return "drop", ("its author isn't in rulesets/teams.json or rulesets/bots.json, and an "
                        "outsider's PR in a public repo is never merged: a member re-creates the "
                        "change as their own PR (design 6.2)")
    if pr["mergeable"] == "CONFLICTING" or pr["mergeStateStatus"] == "DIRTY":
        return "drop", "it conflicts with main: merge main in and resolve it"
    if too_many_checks(pr):
        return "drop", "it has over 100 checks, more than the lane reads: merge it by hand"
    # Before bringing main in: a PR that can't merge anyway isn't worth a CI run.
    states = check_states(pr, required)
    failed = sorted(n for n, s in states.items() if s == "FAIL")
    if failed:
        return "drop", "required checks failed: " + ", ".join(failed)
    waiting_on = people_needed(pr, threads_required)
    if waiting_on:
        return "blocked", "the rules block it: " + waiting_on
    # Before bringing main in: the lane's push would cancel the review, which then starts again
    if review_running(pr):
        return "wait", "the AI review is still running on its last commit"
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
    if (review_missing_for is not None and review_missing_for < REVIEW_GRACE
            and review_state(pr) is None):
        return "wait", "the AI review hasn't started on its last commit yet"
    if pr["mergeStateStatus"] in MERGEABLE:
        return "merge", "approved and green"
    return "blocked", f"the rules block it (GitHub says {pr['mergeStateStatus']})"


def people_needed(pr, threads_required=True):
    """What a person still has to do before it can merge: unresolved conversations (where the rules
    require them resolved), an approval (GitHub's reviewDecision follows the rules)."""
    unresolved = sum(not t["isResolved"] for t in pr["reviewThreads"]["nodes"]) if threads_required else 0
    why = [f"{unresolved} unresolved conversation(s)"] if unresolved else []
    if pr["reviewDecision"] == "REVIEW_REQUIRED":
        why.append("it needs an approval")
    elif pr["reviewDecision"] == "CHANGES_REQUESTED":
        why.append("changes were requested")
    return ", ".join(why)


def already_said(pr, body):
    """True if the lane's last word on the PR is this comment, with no labelling since: a run that
    commented but couldn't take the label off. After a new labelling it is said again."""
    for item in reversed(((pr.get("timelineItems") or {}).get("nodes")) or []):
        if item.get("__typename") == "LabeledEvent" and (item.get("label") or {}).get("name") == LABEL:
            return False
        if item.get("__typename") == "IssueComment" and (item.get("body") or "").startswith("<!-- merge-lane -->"):
            return item["body"] == body
    return False


def drop(gh, pr, reason):
    """Say why, then take the label off (so a failed comment never leaves a silent drop)."""
    who = (pr.get("author") or {}).get("login")
    body = (f"<!-- merge-lane -->\n{'@' + who + ' ' if who else ''}Taken out of the merge lane: {reason}. "
            f"Label it `{LABEL}` again when it's ready (design 6.2).")
    if not already_said(pr, body):
        # not retried: a POST that went through but timed out would post twice; a failure stops
        # the run with the label still on, and the next run says it. A refusal that is about this
        # PR (a locked conversation) doesn't: the label still comes off, or the PR would block the
        # lane, first in every run.
        try:
            gh.call(f"repos/{gh.repo}/issues/{pr['number']}/comments", "POST", {"body": body}, tries=1)
        except urllib.error.HTTPError as e:
            if e.code not in (403, 404, 422) or rate_limited(e):
                raise
            print(f"::warning::#{pr['number']}: couldn't comment ({e.code}); taking the label off anyway")
    try:
        gh.call(f"repos/{gh.repo}/issues/{pr['number']}/labels/{LABEL}", "DELETE")
    except urllib.error.HTTPError as e:
        if e.code != 404:  # already taken off
            raise
    print(f"#{pr['number']}: left the lane ({reason})")


def rate_limited(error):
    """A 403 or 429 that is GitHub's rate limit, not a refusal about the PR."""
    headers = error.headers or {}
    return error.code == 429 or headers.get("x-ratelimit-remaining") == "0" or headers.get("retry-after")


def github_message(error):
    """GitHub's own message from an error response, for the author."""
    try:
        return json.loads(error.read().decode()).get("message", "") or str(error.code)
    except (ValueError, AttributeError, OSError):
        return str(error.code)


def run_one(gh, number, required, deadline, threads_required=True, reviewed=False):
    """Take one PR through the lane. Returns ("merged", author), or "done" once it is dropped or gone, or
    "paused" if the run's deadline came first (it stays labelled, at the front). The timers are
    per run: a paused PR is first in the next run, which gives it CHECKS_TIMEOUT from its start,
    so it waits at most DEADLINE + CHECKS_TIMEOUT in all, plus one CHECKS_TIMEOUT in each run that
    brings main in: a run's first bringing main in starts CHECKS_TIMEOUT again, so waiting for the
    review before it doesn't shorten the checks' time after it (only the first, as MAX_UPDATES is
    counted per run).
    `reviewed`: the repo runs the AI review (GitHub.calls_review)."""
    started, missing_since, blocked_looks, updates, update_errors, refusals = time.time(), {}, 0, 0, 0, 0
    updated_from = None  # (head before the update, when): until GitHub shows the new commit
    waited_on = ""  # the last reason it waited, for the time-limit drop
    no_review_since = {}  # {head: when the lane first saw it with no AI review run}
    while True:
        pr = gh.graphql(PR, number=number)["pullRequest"]
        if pr is None:
            print(f"#{number}: gone (deleted?)")
            return "done"
        head = pr["headRefOid"]
        missing = "MISSING" in check_states(pr, required).values()
        if missing:
            missing_since.setdefault(head, time.time())
        if pr["state"] == "MERGED":
            # merged, though maybe not by a reply we saw (a lost response, then a 405): its issues
            # still need closing, which is harmless if someone else merged it
            print(f"#{number}: merged")
            return "merged", pr.get("author") or {}
        no_review = reviewed and review_state(pr) is None
        if no_review and all(v == "PASS" for v in check_states(pr, required).values()):
            # from when the checks are green, the look that would merge: slow checks don't use it up
            no_review_since.setdefault(head, time.time())
        no_review_for = time.time() - no_review_since[head] if head in no_review_since else 0
        action, reason = decide(pr, required, time.time() - missing_since[head] if missing else 0,
                                threads_required,
                                (no_review_for if no_review else 0) if reviewed else None)
        if updated_from and head != updated_from[0]:
            updated_from = None
        if updated_from and action not in ("gone", "drop"):
            # main was brought in, but GitHub still shows the old commit (and may still say BEHIND):
            # deciding on that would update twice or merge the old head
            if time.time() - updated_from[1] > UPDATE_LAG:
                drop(gh, pr, f"main was brought in, but the new commit didn't appear within "
                             f"{UPDATE_LAG // 60} minutes: bring main in by hand")
                return "done"
            action, reason = "wait", "GitHub to show the commit that brought main in"
        blocked_looks = blocked_looks + 1 if action == "blocked" else 0
        waited_on = reason if action == "wait" else waited_on
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
                updated_from = (head, time.time())
                if updates == 1:  # the checks on the new commit get the whole CHECKS_TIMEOUT
                    started = time.time()
                print(f"#{number}: brought main in; waiting for its checks")
            except urllib.error.HTTPError as e:
                # 422 is about this PR: the head moved (an update already landing) or a conflict
                # GitHub hasn't flagged yet. Look again, and give up only if it keeps failing.
                # Anything else is GitHub's or the lane's, and stops the run (see the top).
                if e.code != 422:
                    raise
                update_errors += 1
                if update_errors >= 3:
                    drop(gh, pr, f"main couldn't be brought in (GitHub said {e.code}): merge it in by hand")
                    return "done"
                print(f"#{number}: bringing main in failed ({e.code}); looking again")
        if action == "merge":
            if no_review:
                print(f"::warning::#{number}: no AI review run appeared on {head} within "
                      f"{REVIEW_GRACE // 60} minutes; merging without it (it is advisory, design 6.4)")
            try:
                gh.call(f"repos/{gh.repo}/pulls/{number}/merge", "PUT",
                        {"merge_method": "squash", "sha": head})
                print(f"#{number}: merged")
                return "merged", pr.get("author") or {}
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
            drop(gh, pr, f"it stayed at the front for over {CHECKS_TIMEOUT // 60} minutes without merging"
                         + (f" (last waiting on: {waited_on})" if waited_on else ""))
            return "done"
        if time.time() > deadline:
            print(f"#{number}: this run's time is up; a new run carries on")
            return "paused"
        time.sleep(POLL)


def can_write(issues_gh, repo, login, cache):
    """Whether `login` may write to `repo`: the lane closes only what the PR's author could close."""
    if (repo, login) not in cache:
        try:
            level = issues_gh.call(f"repos/{repo}/collaborators/{login}/permission")["permission"]
        except urllib.error.HTTPError as e:
            if e.code not in (404, 422):  # not a collaborator there; 422: a bot, which has no access level
                raise
            level = "none"
        cache[(repo, login)] = level in ("admin", "maintain", "write")
    return cache[(repo, login)]


def close_issues(issues_gh, number, author):  # author: {"__typename", "login"}
    """Close the merged PR's open closing issues, then say which PR closed them. Only issues in
    repos the PR's author can write to: the `Closes` text is theirs, and this token reaches every
    repo. Returns what couldn't be closed, for a person to do by hand (the PR is merged either way).
    """
    pr_ref = f"{issues_gh.repo}#{number}"
    try:
        pr = issues_gh.graphql(CLOSING, number=number)["pullRequest"]
        refs = pr["closingIssuesReferences"]["nodes"]
        more = pr["closingIssuesReferences"]["pageInfo"]["hasNextPage"]
    except (OSError, http.client.HTTPException, ValueError, RuntimeError, KeyError, TypeError) as e:
        print(f"::error::#{number} merged, but its closing issues couldn't be read ({e!r})")
        return [f"the issues {pr_ref} closes (couldn't read them)"]
    failed, cache = ([f"the issues {pr_ref} closes beyond the first 50"] if more else []), {}
    bot, login = author.get("__typename") == "Bot", author.get("login")
    for issue in refs:
        if not issue or issue.get("state") != "OPEN":  # null: an issue this token can't see
            continue
        repo, n = issue["repository"]["nameWithOwner"], issue["number"]
        try:
            # a bot's REST login differs ("x[bot]"), and it only opens PRs where it is installed
            allowed = (repo == issues_gh.repo if bot
                       else bool(login) and can_write(issues_gh, repo, login, cache))
            if not allowed:
                print(f"::warning::#{number}: not closing {repo}#{n}: {login} can't write to {repo}")
                failed.append(f"{repo}#{n} ({login} can't write there)")
                continue
            issues_gh.call(f"repos/{repo}/issues/{n}", "PATCH", {"state": "closed", "state_reason": "completed"})
            print(f"#{number}: closed {repo}#{n}")
        except (OSError, http.client.HTTPException, ValueError, RuntimeError, KeyError, TypeError) as e:
            print(f"::error::#{number} merged, but {repo}#{n} couldn't be closed ({e!r}): close it by hand")
            failed.append(f"{repo}#{n}")
            continue
        try:  # closed first: a comment that fails leaves it closed, which is what matters
            issues_gh.call(f"repos/{repo}/issues/{n}/comments", "POST", {"body": (
                f"<!-- merge-lane -->\nClosed by {f'#{number}' if repo == issues_gh.repo else pr_ref}, which the merge lane merged (GitHub doesn't "
                "close a PR's issues when an app merges it; design 6.2).")}, tries=1)
        except (OSError, http.client.HTTPException, ValueError, RuntimeError, KeyError, TypeError) as e:
            print(f"::warning::#{number}: closed {repo}#{n}, but couldn't say why ({e!r})")
    return failed


def report(unclosed, code):
    """The exit code. Issues left open fail the run (so whoever labelled is emailed), and a run with
    PRs still to go says both, so the workflow starts the next run and still fails this one."""
    if not unclosed:
        return code
    print(f"::error::Merged, but close these by hand: {', '.join(unclosed)}")
    return MORE_LEFT_UNCLOSED if code == MORE_LEFT else 1


def main():
    gh = GitHub(os.environ["GITHUB_TOKEN"], os.environ["GITHUB_REPOSITORY"])
    issues_gh = GitHub(os.environ["ISSUES_TOKEN"], os.environ["GITHUB_REPOSITORY"])
    unclosed = []
    try:
        return work(gh, issues_gh, unclosed)
    except BaseException:
        # the run stops on an error with labels left on; still list what a person must close
        if unclosed:
            print(f"::error::Merged, but close these by hand: {', '.join(unclosed)}")
        raise


def work(gh, issues_gh, unclosed):
    """The lane's loop; `unclosed` collects the issues merged PRs left open."""
    required, threads_required = gh.main_rules()
    reviewed = gh.calls_review()
    print(f"AI review: {REVIEW_CALLER} {'is' if reviewed else 'is not'} on main, so the lane "
          f"{'gives a late review run' if reviewed else 'does not wait for a review run to appear'}"
          f"{f' up to {REVIEW_GRACE // 60} minutes' if reviewed else ''}.")
    deadline = time.time() + DEADLINE
    done = set()
    while True:
        waiting = [n for n in queue(gh.graphql(QUEUE, label=LABEL)) if n not in done]
        if not waiting:
            print("The lane is empty.")
            return report(unclosed, 0)
        if time.time() > deadline:
            print(f"Time is up with {len(waiting)} PR(s) still labelled; starting a new run.")
            return report(unclosed, MORE_LEFT)
        print(f"Lane: {', '.join(f'#{n}' for n in waiting)}")
        number = waiting[0]
        # An error here stops the run with every label left on (see the top): it is GitHub's or the
        # lane's, not the PR's, so no author is told to relabel.
        result = run_one(gh, number, required, deadline, threads_required, reviewed)
        if result == "paused":
            return report(unclosed, MORE_LEFT)
        if isinstance(result, tuple):  # ("merged", author)
            unclosed += close_issues(issues_gh, number, result[1])
        done.add(number)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Monthly AI review scorecard (design 6.4): one issue per month in this repo.

Usage: scripts/ai_scorecard.py collect <out.json> [YYYY-MM]   (default: last month)
       scripts/ai_scorecard.py report <out.json>

`collect` reads every PR merged that month in the managed repos (rulesets/repos.txt): the AI
review's own reviews (scripts/ai_review.py) and its threads, each thread's severity, round and the
outcome its replies give. It needs a token that reads pull requests and contents in every repo.
`report` files the month's issue as the automation app, so the issue-fields workflow puts it on the
board, assigned to the automation owners.

What it reports (design 6.4):
- late catches: critical or major threads after round one whose code was already there in round
  one: the last lines of the comment's diff hunk (up to the commented line) appear, in order, as
  whole lines of that file at round one's commit. Lines too short to tell (`}`, `return x`) don't
  count. Target near zero.
- false alarms: critical or major threads whose outcome is "not an issue".
- rounds per PR: the reviews that finished (not a cut-off or too-large note).
- cost per PR: from the tokens each review records (its usage marker), at PRICES.
- threads with no readable outcome: resolved without a reply that starts with Fixed, Not an issue
  or Moved to (the reply the reviewer asks for on every thread).
Threads from before the severity scale (2026-10-10, .github#106) are unrated: they are counted, and
left out of the rest, since they have no severity and no reply convention.
"""

import base64
import datetime
import json
import re
import subprocess
import os
import sys

from one_issue import keep
from rules import ORG, managed_repos

REPO = f"{ORG}/.github"
LABEL = "ai-scorecard"
REVIEWER = {"github-actions", "github-actions[bot]"}  # who posts the reviews (GraphQL drops [bot])
MARKER = "<!-- ai-review -->"
USAGE_MARKER = re.compile(r"<!-- ai-review-usage: (\{[^<>]*\}) -->")
SEVERITY = re.compile(r"\*\*(Critical|Major|Minor|Nit)\*\*")
SERIOUS = {"critical", "major"}
OUTCOMES = (("fixed", "fixed"), ("not an issue", "not an issue"), ("moved to", "moved"))
# The notes ai_review.py posts instead of a review start with one of these, after the header
NOT_A_ROUND = ("The review was cut off", "Not reviewed: the diff is too large")
WINDOW = 3  # hunk lines, up to the commented one, that must all be at round one's commit
MIN_CHARS = 25  # shorter than this together (`}`, `return x`) is in most files: can't tell
# USD per million tokens: input, cache write (1 hour), cache read, output. Matched by prefix, so a
# dated snapshot id prices too. platform.claude.com/docs/en/about-claude/pricing, read 2026-10-10
# (Opus 5.5's cache reads are 0.05x input, not the usual 0.1x).
PRICES = {"claude-opus-5-5": (4.0, 8.0, 0.20, 20.0)}

PRS_QUERY = """query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 50, after: $after) { pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest { number url repository { name } } } } }"""
PR_QUERY = """query($owner: String!, $name: String!, $n: Int!) {
  repository(owner: $owner, name: $name) { pullRequest(number: $n) {
    reviews(first: 100) { nodes { id author { login } body submittedAt commit { oid } } }
    reviewThreads(first: 100) { nodes { isResolved path
      comments(first: 30) { nodes { author { login } body url diffHunk pullRequestReview { id } } } } } } } }"""


def gh_json(*args):
    res = subprocess.run([os.environ.get("GH", "gh"), "api", *args], capture_output=True, text=True)
    if res.returncode != 0:
        sys.exit(f"gh api {' '.join(args[:2])} failed:\n{res.stderr.strip()}")
    return json.loads(res.stdout)


def graphql(query, **variables):
    args = ["graphql", "-f", f"query={query}"]
    for k, v in variables.items():
        if v is not None:
            args += ["-F" if isinstance(v, int) else "-f", f"{k}={v}"]
    return gh_json(*args)["data"]


def last_month(today):
    first = today.replace(day=1) - datetime.timedelta(days=1)
    return first.strftime("%Y-%m")


def month_range(month):
    start = datetime.date.fromisoformat(f"{month}-01")
    end = (start + datetime.timedelta(days=32)).replace(day=1) - datetime.timedelta(days=1)
    return f"{start}..{end}"


def severity(body):
    found = SEVERITY.match((body or "").strip())
    return found.group(1).lower() if found else "unrated"  # threads from before 2026-10-10


def outcome(replies):
    """The outcome the first reply that gives one states, or None."""
    for text in replies:
        words = (text or "").strip().lstrip("*_ ").lower()
        for start, name in OUTCOMES:
            if words.startswith(start):
                return name
    return None


def commented_lines(diff_hunk):
    """The last WINDOW lines of the new side of a comment's diff hunk, up to the commented line,
    stripped; blank lines left out. Empty if the comment is on a removed line."""
    lines = (diff_hunk or "").splitlines()
    if not lines or lines[-1][:1] not in "+ ":
        return []
    new = [l[1:].strip() for l in lines[1:] if l[:1] in "+ "]
    return [l for l in new if l][-WINDOW:]


def was_there(lines, text):
    """True if `lines` appear in order as whole lines of `text` (blank lines ignored), and are long
    enough together to tell."""
    if sum(len(l) for l in lines) < MIN_CHARS:
        return False
    have = [l.strip() for l in text.splitlines() if l.strip()]
    return any(have[i:i + len(lines)] == lines for i in range(len(have) - len(lines) + 1))


def is_round(body):
    """A finished review, not a cut-off or too-large note (those don't count as rounds)."""
    if body.startswith("**AI review (advisory), round"):
        return True
    after = body.split("\n\n", 1)[1] if "\n\n" in body else ""
    return not after.startswith(NOT_A_ROUND)  # the reviewer before 2026-10-10 had no round number


def usage_of(body):
    """The tokens a review recorded (its last usage marker), or None."""
    found = USAGE_MARKER.findall(body)
    try:
        usage = json.loads(found[-1]) if found else None
    except json.JSONDecodeError:
        return None
    return usage if isinstance(usage, dict) else None


def cost(usage):
    """USD for one review's recorded usage, or None for a model without a price."""
    model = usage.get("requested") or usage.get("model") or ""
    price = next((p for key, p in PRICES.items() if model.startswith(key)), None)
    if not price:
        return None
    tokens = (usage.get("input", 0), usage.get("cache_write", 0), usage.get("cache_read", 0),
              usage.get("output", 0))
    return sum(t * p for t, p in zip(tokens, price)) / 1_000_000


def file_at(repo, sha, path):
    """A file's text at a commit, or None if it wasn't there (or this token can't read it)."""
    res = subprocess.run([os.environ.get("GH", "gh"), "api",
                          f"repos/{ORG}/{repo}/contents/{path}?ref={sha}"], capture_output=True, text=True)
    if res.returncode != 0:
        return None
    return base64.b64decode(json.loads(res.stdout).get("content", "")).decode(errors="replace")


def pr_record(repo, number, url, data, read=file_at):
    """One merged PR's review data: rounds, cost and threads. None if the AI review never ran on it."""
    reviews = sorted((r for r in data["reviews"]["nodes"]
                      if (r.get("author") or {}).get("login") in REVIEWER
                      and (r.get("body") or "").startswith("**AI review (advisory)")),
                     key=lambda r: r["submittedAt"])
    if not reviews:
        return None
    rounds = [r for r in reviews if is_round(r["body"])]
    round_of = {r["id"]: i + 1 for i, r in enumerate(rounds)}
    costs = [cost(u) for u in map(usage_of, (r["body"] for r in reviews)) if u]
    first_commit = rounds[0]["commit"]["oid"] if rounds else None
    seen = {}  # path -> its text at round one's commit
    threads = []
    for t in data["reviewThreads"]["nodes"]:
        comments = t["comments"]["nodes"]
        if not comments or MARKER not in (comments[0]["body"] or ""):
            continue
        first = comments[0]
        level = severity(first["body"])
        round_no = round_of.get((first.get("pullRequestReview") or {}).get("id"), 1)
        late = False
        if round_no > 1 and level in SERIOUS and first_commit:
            lines = commented_lines(first.get("diffHunk"))
            if lines:
                if t["path"] not in seen:
                    seen[t["path"]] = read(repo, first_commit, t["path"]) or ""
                late = was_there(lines, seen[t["path"]])
        replies = [c["body"] for c in comments[1:] if (c.get("author") or {}).get("login") not in REVIEWER]
        threads.append({"url": first["url"], "severity": level, "round": round_no, "late": late,
                        "resolved": t["isResolved"], "outcome": outcome(replies)})
    return {"repo": repo, "number": number, "url": url, "rounds": len(rounds),
            "cost": sum(costs) if costs and None not in costs else None, "threads": threads}


def collect(path, month):
    found, after = [], None
    while True:
        page = graphql(PRS_QUERY, q=f"org:{ORG} is:pr is:merged merged:{month_range(month)}",
                       after=after)["search"]
        found += [n for n in page["nodes"] if n]
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    repos = set(managed_repos())
    prs = []
    for n in found:
        repo = n["repository"]["name"]
        if repo not in repos:
            continue
        data = graphql(PR_QUERY, owner=ORG, name=repo, n=n["number"])["repository"]["pullRequest"]
        record = pr_record(repo, n["number"], n["url"], data)
        if record:
            prs.append(record)
    with open(path, "w") as f:
        json.dump({"month": month, "prs": prs}, f, indent=1)
    print(f"{month}: {len(prs)} merged PR(s) with an AI review, of {len(found)} merged")


def score(prs):
    every = [dict(t, pr=f"{p['repo']}#{p['number']}") for p in prs for t in p["threads"]]
    threads = [t for t in every if t["severity"] != "unrated"]
    serious = [t for t in threads if t["severity"] in SERIOUS]
    costs = [p["cost"] for p in prs if p["cost"] is not None]
    rounds = [p["rounds"] for p in prs]
    return {
        "prs": len(prs),
        "late": [t for t in threads if t["late"]],
        "serious": len(serious),
        "false_alarms": [t for t in serious if t["outcome"] == "not an issue"],
        "unreadable": [t for t in threads if t["resolved"] and not t["outcome"]],
        "rounds_mean": sum(rounds) / len(rounds) if rounds else 0,
        "rounds_max": max(rounds, default=0),
        "long": [p for p in prs if p["rounds"] >= 4],
        "cost_total": sum(costs),
        "cost_mean": sum(costs) / len(costs) if costs else None,
        "costed": len(costs),
        "unrated": len(every) - len(threads),
    }


def links(items, text):
    return "\n".join(f"- [{text(i)}]({i['url']})" for i in items) or "- none"


def body(month, s):
    mean_cost = f"${s['cost_mean']:.2f}" if s["cost_mean"] is not None else "n/a"
    unrated = (f"\n\n{s['unrated']} thread(s) from the reviewer before 2026-10-10 have no severity "
               "and are left out." if s["unrated"] else "")
    return f"""### What

The AI review scorecard for {month} (design 6.4), from {s['prs']} merged PR(s) the review ran on.

| Measure | {month} | Target |
|---|---|---|
| Late catches: critical or major after round one, in code round one saw | {len(s['late'])} | near 0 |
| False alarms: critical or major closed as not an issue | {len(s['false_alarms'])} of {s['serious']} | near 0 |
| Rounds per PR | {s['rounds_mean']:.1f} mean, {s['rounds_max']} most | about 2 |
| Cost per PR | {mean_cost} mean, ${s['cost_total']:.2f} in all ({s['costed']} of {s['prs']} PRs recorded it) | |
| Threads resolved with no readable outcome | {len(s['unreadable'])} | 0 |{unrated}

Late catches:
{links(s['late'], lambda t: f"{t['pr']}, round {t['round']}, {t['severity']}")}

False alarms:
{links(s['false_alarms'], lambda t: f"{t['pr']}, round {t['round']}, {t['severity']}")}

Round 4 or later:
{links(s['long'], lambda p: f"{p['repo']}#{p['number']}, {p['rounds']} rounds")}

No readable outcome (the reply should start with Fixed, Not an issue or Moved to):
{links(s['unreadable'], lambda t: f"{t['pr']}, {t['severity']}")}

### Why

To tell whether the reviewer finds the real problems in round one without inventing problems, and
when to change it (design 6.4): more late catches mean more context, or two reviewers in round
one; more false alarms mean a tighter bar.

### Done when

- A person reads this and decides whether the reviewer changes (an issue for the change, if so),
  then closes it

### Discipline

Software

### Priority

Medium

### Links

Design 6.4; `scripts/ai_scorecard.py`, `.github/workflows/ai-scorecard.yml`

### Design decisions

_No response_"""


def report(path):
    data = json.load(open(path))
    month = data["month"]
    print(keep(REPO, LABEL, f"AI review scorecard: {month}", body(month, score(data["prs"])),
               title_match=True))


def main(argv):
    if argv[:1] == ["collect"] and len(argv) in (2, 3):
        month = argv[2] if len(argv) == 3 else last_month(datetime.date.today())
        if not re.fullmatch(r"\d{4}-\d{2}", month):
            sys.exit(f"not a month: {month} (YYYY-MM)")
        return collect(argv[1], month)
    if argv[:1] == ["report"] and len(argv) == 2:
        return report(argv[1])
    sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])

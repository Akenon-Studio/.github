#!/usr/bin/env python3
"""Monthly AI review scorecard (design 6.4): one issue per month in this repo.

Usage: scripts/ai_scorecard.py collect <out.json> [YYYY-MM]   (default: last month)
       scripts/ai_scorecard.py report <out.json>

`collect` reads every PR merged that month in the managed repos (rulesets/repos.txt): the AI
review's own reviews (scripts/ai_review.py) and its threads, each thread's severity, round and the
outcome its replies give; the local-review section of its description (review-branch.py's, between
LOCAL_START and LOCAL_END); and, for a `fix` PR, the reviewed PRs whose lines it changes. It needs a
token that reads pull requests and contents in every repo.
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
- each outcome's share by round, for local and CI review apart: of the critical and major findings
  (CI's threads, the local runs' rows), the severity after re-grading (kept, downgraded, raised) and
  the outcome, counted apart, so a finding downgraded and then fixed counts in both. A local row
  gives both; a CI reply gives the outcome and, if it says one (`re-graded minor`, `downgraded to
  minor`), the new severity, else the severity is kept. An agent's re-grade is a dispute, counted
  only once settled: in a local row, review-branch.py marks it "(an agent's, settled)" or "(an
  agent's, not settled)", whoever authored the PR; in a thread, a reply carrying the agent marker
  (`<!-- agent -->`), or the Peras app's, states an outcome other than Fixed, and only a person's
  reply that states one settles it. Disputed findings, those with no outcome, and on a Peras PR the
  findings Peras fixed with no person's re-grade (ungraded, design 6.4) are listed, not counted.
- escapes: a `fix` PR whose merge changes (removes or rewrites) lines that a reviewed PR (an AI
  review or a local-review section) last changed in the ESCAPE_DAYS before it merged, by blame at
  the fix's parent commit, with that review's findings and outcomes in the files concerned. A fix
  that only adds lines changes none, so it isn't one.
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
from rules import ORG, acts_for, bot_login, managed_repos

REPO = f"{ORG}/.github"
LABEL = "ai-scorecard"
REVIEWER = {"github-actions", "github-actions[bot]"}  # who posts the reviews (GraphQL drops [bot])
MARKER = "<!-- ai-review -->"
USAGE_MARKER = re.compile(r"<!-- ai-review-usage: (\{[^<>]*\}) -->")
SEVERITY = re.compile(r"\*\*(Critical|Major|Minor|Nit)\*\*")
SERIOUS = {"critical", "major"}
OUTCOMES = (("fixed", "fixed"), ("not an issue", "not an issue"), ("moved to", "moved"))
RANK = {"nit": 0, "minor": 1, "major": 2, "critical": 3}
# A reply's new severity: `re-graded minor`, `downgraded to minor`, `raised to critical`
REGRADE = re.compile(r"\b(?:re-?grad|downgrad|rais)\w*\s*(?:(?:to|as)\b\s*)?:?\s*\**(critical|major|minor|nit)\b",
                     re.I)
AGENT = "<!-- agent -->"  # what an agent posts under its person's login carries (design 6.4)
PERAS = "akenon-studio-peras"  # the Peras app (rulesets/bots.json); its PRs and replies are an agent's
# review-branch.py's section in a PR's description (design 6.4); ai_review.py skips the same markers
LOCAL_START = "<!-- local-review:start -->"
LOCAL_END = "<!-- local-review:end -->"
LOCAL_ID = re.compile(r"R(\d+)\.\d+")
LOCAL_OUTCOMES = {"fixed": "fixed", "issue": "issue", "future": "future", "not-an-issue": "not an issue"}
# Every outcome, in the report's order: local runs give issue or future, CI's replies "Moved to" either
SHARE_OUTCOMES = ("fixed", "issue", "future", "moved", "not an issue")
GRADES = ("kept", "downgraded", "raised")
FIX_TITLE = re.compile(r"fix(\([^)]*\))?!?:")
ESCAPE_DAYS = 90
HUNK_HEAD = re.compile(r"@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@")
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
    title body mergedAt author { __typename login } editor { __typename login }
    mergeCommit { oid parents(first: 1) { nodes { oid } } }
    reviews(first: 100) { nodes { id author { login } body submittedAt commit { oid } } }
    reviewThreads(first: 100) { nodes { isResolved path
      comments(first: 30) { nodes { author { __typename login } body url diffHunk pullRequestReview { id } } } } } } } }"""
BLAME_QUERY = """query($owner: String!, $name: String!, $oid: GitObjectID!, $path: String!) {
  repository(owner: $owner, name: $name) { object(oid: $oid) { ... on Commit { blame(path: $path) {
    ranges { startingLine endingLine commit { oid
      associatedPullRequests(first: 5) { nodes { number url mergedAt } } } } } } } } }"""


def gh_json(*args, fail=True):
    """The JSON `gh api` returns; on failure, exits, or None when `fail` is False."""
    res = subprocess.run([os.environ.get("GH", "gh"), "api", *args], capture_output=True, text=True)
    if res.returncode != 0:
        if not fail:
            return None
        sys.exit(f"gh api {' '.join(args[:2])} failed:\n{res.stderr.strip()}")
    return json.loads(res.stdout)


def graphql(query, fail=True, **variables):
    """A GraphQL query's data; a failed one exits, or gives None when `fail` is False (a file blame
    can't read)."""
    args = ["graphql", "-f", f"query={query}"]
    for k, v in variables.items():
        if v is not None:
            args += ["-F" if isinstance(v, int) else "-f", f"{k}={v}"]
    found = gh_json(*args, fail=fail)
    return found["data"] if found else None


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


def is_agent(comment):
    """A reply an agent posted: under its person's login with the agent marker, or as the Peras app."""
    author = comment.get("author") or {}
    return AGENT in (comment.get("body") or "") or bot_login(author.get("login")) == PERAS


def regraded(text):
    """The severity a reply re-grades a finding to (`re-graded minor`), or None."""
    found = REGRADE.search(text or "")
    return found.group(1).lower() if found else None


def thread_outcome(replies, peras_pr):
    """A CI thread's counted outcome from the org's replies (design 6.4): {outcome, regraded,
    disputed, ungraded}. A person's reply that states an outcome is their own call. An agent's Fixed
    counts (the next round checks it); an agent's other outcome is a dispute, counted only once a
    person's reply states one. On a Peras PR, a finding Peras fixed is ungraded until a person's
    reply gives its severity after re-grading."""
    person = [c for c in replies if not is_agent(c)]
    said = next(((c, o) for c in person for o in [outcome([c["body"]])] if o), None)
    if said:
        return {"outcome": said[1], "regraded": regraded(said[0]["body"]), "disputed": False,
                "ungraded": False}
    claimed = next(((c, o) for c in replies if is_agent(c) for o in [outcome([c["body"]])] if o), None)
    if not claimed:
        return {"outcome": None, "regraded": None, "disputed": False, "ungraded": False}
    c, name = claimed
    if name != "fixed":
        return {"outcome": name, "regraded": regraded(c["body"]), "disputed": True, "ungraded": False}
    graded = next((g for g in map(regraded, (x["body"] for x in person)) if g), None)
    if peras_pr and bot_login((c.get("author") or {}).get("login")) == PERAS:
        return {"outcome": "fixed", "regraded": graded, "disputed": False, "ungraded": graded is None}
    return {"outcome": "fixed", "regraded": graded or regraded(c["body"]), "disputed": False, "ungraded": False}


def cells(row):
    """A markdown table row's cells, with `\\|` read as a pipe inside a cell."""
    parts = re.split(r"(?<!\\)\|", row.strip())
    return [x.strip().replace("\\|", "|") for x in parts[1:-1]]


def local_findings(description):
    """The critical and major findings review-branch.py wrote into a PR's local-review section (design
    6.4): run, claimed severity, severity after re-grading, outcome, and whether an agent's re-grade
    is still a dispute (`(an agent's, not settled)`). A section with no end runs to the description's
    end."""
    found, rest = [], description or ""
    while LOCAL_START in rest:
        text, _, after = rest.partition(LOCAL_START)[2].partition(LOCAL_END)
        rest = after
        for line in text.splitlines():
            row = cells(line) if line.strip().startswith("|") else []
            if len(row) < 5:
                continue
            ident = LOCAL_ID.fullmatch(row[0])
            claimed = row[2].lower()
            if not ident or claimed not in ("critical", "major"):
                continue
            words = row[4].split()
            name = LOCAL_OUTCOMES.get(words[0].lower()) if words else None
            again = row[3].lower() if row[3].lower() in RANK else None
            found.append({"id": row[0], "round": int(ident.group(1)), "severity": claimed,
                          "regraded": again, "outcome": name,
                          "disputed": bool(name) and "not settled" in row[4], "ungraded": False,
                          "path": row[1].strip("`").rpartition(":")[0] or row[1].strip("`")})
    return found


def readable(data):
    """Whether the PR's description is the org's own text (design 6.2): its author and its last
    editor are the org's people or apps."""
    return acts_for(data.get("author"), private=False) and (
        not data.get("editor") or acts_for(data.get("editor"), private=False))


def pr_record(repo, number, url, data, read=file_at):
    """One merged PR's review data: rounds, cost, threads and the local runs' findings. None if
    neither the AI review nor a local run left anything on it."""
    reviews = sorted((r for r in data["reviews"]["nodes"]
                      if (r.get("author") or {}).get("login") in REVIEWER
                      and (r.get("body") or "").startswith("**AI review (advisory)")),
                     key=lambda r: r["submittedAt"])
    ours = readable(data)
    local = local_findings(data.get("body")) if ours else []
    if not reviews and not (ours and LOCAL_START in (data.get("body") or "")):
        return None
    peras_pr = bot_login((data.get("author") or {}).get("login")) == PERAS
    rounds = [r for r in reviews if is_round(r["body"])]
    round_of = {r["id"]: i + 1 for i, r in enumerate(rounds)}
    costs = [cost(u) for u in map(usage_of, (r["body"] for r in reviews)) if u]
    first_commit = rounds[0]["commit"]["oid"] if rounds else None
    seen = {}  # path -> its text at round one's commit
    threads = []
    for t in data["reviewThreads"]["nodes"]:
        comments = t["comments"]["nodes"]
        if (not comments or MARKER not in (comments[0]["body"] or "")
                or (comments[0].get("author") or {}).get("login") not in REVIEWER):
            continue  # design 6.2: a thread anyone else opened, marker or not, isn't the review's
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
        # only the org's people and apps answer a finding (design 6.2): an outsider's reply is not
        # counted, in any repo (a private one has no outsiders to drop)
        replies = [c for c in comments[1:]
                   if (c.get("author") or {}).get("login") not in REVIEWER
                   and acts_for(c.get("author"), private=False)]
        threads.append(dict(thread_outcome(replies, peras_pr), url=first["url"], severity=level,
                            round=round_no, late=late, resolved=t["isResolved"], path=t.get("path")))
    return {"repo": repo, "number": number, "url": url, "rounds": len(rounds),
            "cost": sum(costs) if costs and None not in costs else None, "threads": threads,
            "local": local}


def removed_lines(patch):
    """The old side's line numbers a file's patch removes or rewrites (an added line moves none)."""
    out, old = [], 0
    for line in (patch or "").splitlines():
        head = HUNK_HEAD.match(line)
        if head:
            old = int(head.group(1))
        elif line.startswith("-"):
            out.append(old)
            old += 1
        elif line.startswith(" "):
            old += 1
    return out


def blamed_prs(ranges, lines):
    """{number: the merged PR, with how many of `lines` it last changed}, from a file's blame."""
    out = {}
    for r in ranges or []:
        n = sum(r["startingLine"] <= line <= r["endingLine"] for line in lines)
        nodes = ((r.get("commit") or {}).get("associatedPullRequests") or {}).get("nodes") or []
        pr = next((x for x in nodes if x and x.get("mergedAt")), None)
        if n and pr:
            out.setdefault(pr["number"], dict(pr, lines=0))["lines"] += n
    return out


def when(stamp):
    return datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def finding_text(f):
    """One finding of a reviewed PR as the escapes list shows it."""
    name = f"local {f['id']}" if f["kind"] == "local" else f"CI round {f['round']}"
    grade = f" re-graded {f['regraded']}" if f.get("regraded") and f["regraded"] != f["severity"] else ""
    state = " (disputed)" if f.get("disputed") else " (ungraded)" if f.get("ungraded") else ""
    return f"{name} {f['severity']}{grade}: {f.get('outcome') or 'no outcome'}{state}"


def escapes(repo, fix, files, blame, reviewed):
    """The escapes a merged `fix` PR shows (design 6.4): each reviewed PR that last changed lines the
    fix's merge removes or rewrites, merged within ESCAPE_DAYS before the fix, with that review's
    findings in the files concerned. `files` are the merge commit's (REST, against its parent),
    `blame(path)` the file's blame ranges at the parent, `reviewed(number)` a PR's record or None."""
    hits = {}
    for f in files:
        lines = [] if f.get("status") == "added" else removed_lines(f.get("patch"))
        if not lines:
            continue
        path = f.get("previous_filename") or f["filename"]
        for number, pr in blamed_prs(blame(path), lines).items():
            age = when(fix["mergedAt"]) - when(pr["mergedAt"])
            if number == fix["number"] or not datetime.timedelta(0) <= age <= datetime.timedelta(days=ESCAPE_DAYS):
                continue
            hit = hits.setdefault(number, {"pr": pr, "lines": 0, "paths": []})
            hit["lines"] += pr["lines"]
            hit["paths"].append(path)
    out = []
    for number, hit in sorted(hits.items()):
        record = reviewed(number)
        if not record:
            continue
        findings = [finding_text(f) for f in findings_of([record]) if f.get("path") in hit["paths"]]
        out.append({"fix": f"{repo}#{fix['number']}", "url": fix["url"], "pr": f"{repo}#{number}",
                    "pr_url": hit["pr"]["url"], "lines": hit["lines"], "paths": sorted(set(hit["paths"])),
                    "findings": findings})
    return out


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
    prs, escaped, cache = [], [], {}

    def reviewed(repo, number):
        if (repo, number) not in cache:
            data = graphql(PR_QUERY, owner=ORG, name=repo, n=number)["repository"]["pullRequest"]
            cache[repo, number] = pr_record(repo, number, "", data, lambda *a: "")
        return cache[repo, number]

    def blame(repo, sha, path):
        data = graphql(BLAME_QUERY, owner=ORG, name=repo, oid=sha, path=path, fail=False)
        return ((((data or {}).get("repository") or {}).get("object") or {}).get("blame") or {}).get("ranges") or []

    for n in found:
        repo = n["repository"]["name"]
        if repo not in repos:
            continue
        data = graphql(PR_QUERY, owner=ORG, name=repo, n=n["number"])["repository"]["pullRequest"]
        record = pr_record(repo, n["number"], n["url"], data)
        if record:
            prs.append(record)
        merge = data.get("mergeCommit") or {}
        parents = (merge.get("parents") or {}).get("nodes") or []
        if FIX_TITLE.match(data.get("title") or "") and parents:
            # a fix PR is small; the commit API lists its first 300 files, which is plenty
            files = gh_json(f"repos/{ORG}/{repo}/commits/{merge['oid']}").get("files") or []
            fix = {"number": n["number"], "url": n["url"], "mergedAt": data["mergedAt"]}
            escaped += escapes(repo, fix, files, lambda p, r=repo, sha=parents[0]["oid"]: blame(r, sha, p),
                               lambda m, r=repo: reviewed(r, m))
    with open(path, "w") as f:
        json.dump({"month": month, "prs": prs, "escapes": escaped}, f, indent=1)
    print(f"{month}: {len(prs)} merged PR(s) a review ran on, of {len(found)} merged; {len(escaped)} escape(s)")


def findings_of(prs):
    """Every critical and major finding of the PRs: CI's threads and the local runs' rows."""
    for p in prs:
        name = f"{p['repo']}#{p['number']}"
        for t in p["threads"]:
            if t["severity"] in SERIOUS:
                yield dict(t, kind="CI", pr=name)
        for f in p.get("local", []):
            yield dict(f, kind="local", pr=name, url=p["url"])


def grade(f):
    after = f.get("regraded") or f["severity"]
    return "kept" if RANK[after] == RANK[f["severity"]] else "downgraded" if RANK[after] < RANK[f["severity"]] else "raised"


def shares(prs):
    """{(review, round): counts}: findings; the counted ones' severity after re-grading and outcome;
    and the disputed, ungraded and outcome-less ones, which aren't counted (design 6.4)."""
    table = {}
    for f in findings_of(prs):
        row = table.setdefault((f["kind"], f["round"]), dict.fromkeys(
            ("findings", "counted", *GRADES, *SHARE_OUTCOMES, "disputed", "no outcome", "ungraded"), 0))
        row["findings"] += 1
        if f.get("ungraded"):
            row["ungraded"] += 1
        elif f.get("disputed"):
            row["disputed"] += 1
        elif not f.get("outcome"):
            row["no outcome"] += 1
        else:
            row["counted"] += 1
            row[f["outcome"]] += 1
            row[grade(f)] += 1
    return dict(sorted(table.items(), key=lambda kv: (kv[0][0] != "local", kv[0][1])))


def score(prs, escaped=()):
    every = [dict(t, pr=f"{p['repo']}#{p['number']}") for p in prs for t in p["threads"]]
    threads = [t for t in every if t["severity"] != "unrated"]
    serious = [t for t in threads if t["severity"] in SERIOUS]
    costs = [p["cost"] for p in prs if p["cost"] is not None]
    rounds = [p["rounds"] for p in prs if p["rounds"]]
    findings = list(findings_of(prs))
    return {
        "prs": len(prs),
        "ci_prs": len(rounds),
        "late": [t for t in threads if t["late"]],
        "serious": len(serious),
        "false_alarms": [t for t in serious if t["outcome"] == "not an issue"
                         and not t.get("disputed") and not t.get("ungraded")],
        "unreadable": [t for t in threads if t["resolved"] and not t["outcome"]],
        "rounds_mean": sum(rounds) / len(rounds) if rounds else 0,
        "rounds_max": max(rounds, default=0),
        "long": [p for p in prs if p["rounds"] >= 4],
        "cost_total": sum(costs),
        "cost_mean": sum(costs) / len(costs) if costs else None,
        "costed": len(costs),
        "unrated": len(every) - len(threads),
        "shares": shares(prs),
        "disputed": [f for f in findings if f.get("disputed") and not f.get("ungraded")],
        "ungraded": [f for f in findings if f.get("ungraded")],
        "escapes": list(escaped),
    }


def share(n, of):
    return f"{n} ({100 * n / of:.0f}%)" if of else str(n)


def shares_table(table):
    head = ("| Review | Round | Findings | Counted | Kept | Downgraded | Raised | Fixed | Issue | Future | Moved "
            "| Not an issue | Disputed | No outcome | Ungraded |\n|" + "---|" * 15)
    rows = [f"| {kind} | {n} | {r['findings']} | {r['counted']} | "
            + " | ".join(share(r[k], r["counted"]) for k in (*GRADES, *SHARE_OUTCOMES))
            + f" | {r['disputed']} | {r['no outcome']} | {r['ungraded']} |" for (kind, n), r in table.items()]
    return "\n".join([head, *rows]) if rows else "No critical or major findings."


def escape_lines(items):
    return "\n".join(f"- [{e['fix']}]({e['url']}) changes {e['lines']} line(s) [{e['pr']}]({e['pr_url']}) last "
                     f"changed ({', '.join(f'`{p}`' for p in e['paths'])}); its findings there: "
                     f"{'; '.join(e['findings']) or 'none'}" for e in items) or "- none"


def links(items, text):
    return "\n".join(f"- [{text(i)}]({i['url']})" for i in items) or "- none"


def body(month, s):
    mean_cost = f"${s['cost_mean']:.2f}" if s["cost_mean"] is not None else "n/a"
    unrated = (f"\n\n{s['unrated']} thread(s) from the reviewer before 2026-10-10 have no severity "
               "and are left out." if s["unrated"] else "")
    return f"""### What

The AI review scorecard for {month} (design 6.4), from {s['prs']} merged PR(s) a review ran on
({s['ci_prs']} in CI).

| Measure | {month} | Target |
|---|---|---|
| Late catches: critical or major after round one, in code round one saw | {len(s['late'])} | near 0 |
| False alarms: critical or major closed as not an issue | {len(s['false_alarms'])} of {s['serious']} | near 0 |
| CI rounds per PR | {s['rounds_mean']:.1f} mean, {s['rounds_max']} most | about 2 |
| Cost per PR | {mean_cost} mean, ${s['cost_total']:.2f} in all ({s['costed']} of {s['prs']} PRs recorded it) | |
| Threads resolved with no readable outcome | {len(s['unreadable'])} | 0 |
| Escapes: `fix` PRs changing lines a reviewed PR last changed in the {ESCAPE_DAYS} days before | {len(s['escapes'])} | near 0 |{unrated}

Each outcome's share by round, of the critical and major findings counted: those with an outcome,
an agent's re-grade only once settled. The severity after re-grading (kept, downgraded, raised) and
the outcome are counted apart. Local review's outcomes come from the PR's local-review section, CI's
from the thread replies; disputed, ungraded and outcome-less findings are listed, not counted.

{shares_table(s['shares'])}

Late catches:
{links(s['late'], lambda t: f"{t['pr']}, round {t['round']}, {t['severity']}")}

False alarms:
{links(s['false_alarms'], lambda t: f"{t['pr']}, round {t['round']}, {t['severity']}")}

Round 4 or later:
{links(s['long'], lambda p: f"{p['repo']}#{p['number']}, {p['rounds']} rounds")}

No readable outcome (the reply should start with Fixed, Not an issue or Moved to):
{links(s['unreadable'], lambda t: f"{t['pr']}, {t['severity']}")}

Disputed, not yet settled by a person:
{links(s['disputed'], lambda f: f"{f['pr']}, {f['kind']} round {f['round']}, {f['severity']}, {f['outcome']}")}

Ungraded (Peras fixed them; no person's re-grade yet):
{links(s['ungraded'], lambda f: f"{f['pr']}, {f['kind']} round {f['round']}, {f['severity']}")}

Escapes:
{escape_lines(s['escapes'])}

### Why

To tell whether the reviewer finds the real problems in round one without inventing problems, and
when to change it (design 6.4): more late catches or escapes mean more context, or two reviewers in
round one; more false alarms, or a round whose findings are mostly downgraded or not an issue, mean
a tighter bar or fewer runs.

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
    print(keep(REPO, LABEL, f"AI review scorecard: {month}", body(month, score(data["prs"], data.get("escapes", []))),
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

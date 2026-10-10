#!/usr/bin/env python3
"""Check that a PR closes at least one issue, and only whole issues (design 6.8, checks 1 and 2).

The PR body must say `Closes #n` (or Fixes / Resolves, any tense, also `owner/repo#n` or an issue
URL) for at least one issue: only those forms make GitHub close the issue on merge, so only they
count. A PR that says it is "part of" (or "partly", "partially", "towards") an issue it doesn't
close fails: that work splits the issue first, and the PR closes the new sub-issue. Quoted text
(HTML comments, code) is not read.

A PR from a bot with no issue behind it (`no_issue` in rulesets/bots.json: Renovate's dependency
updates, release-please's Release PR) passes without one, as long as every commit on it is that
bot's own: authored by the bot, committed by the bot or by GitHub for it (`web-flow`), and verified.
GitHub checks the signature against the committer, so a person can't pass by forging only the
author. A person who pushes their work onto the bot's branch needs `Closes #n` like anyone. Merge
commits GitHub makes to bring the base branch in (the merge lane's update-branch call, the Update
branch button: committed by `web-flow`, verified) are skipped; any other merge commit counts like a
normal one, since a merge can carry changes of its own. Its source label says where it came from
(design 6.8, check 1).

Usage: scripts/pr_closes_issue.py      (reads PR_BODY, PR_AUTHOR, PR_AUTHOR_TYPE, PR_NUMBER and
GITHUB_REPOSITORY from the environment; GH_TOKEN reads the PR's commits)
In Actions it runs as the `closes-issue` job of the reusable pr-title workflow.
"""

import json
import os
import re
import subprocess
import sys

from rules import bot_login, bot_sources

REF = (r"(?:https://github\.com/(?P<url_repo>[\w.-]+/[\w.-]+)/issues/(?P<url_n>\d+)"
       r"|(?P<repo>[\w.-]+/[\w.-]+)?#(?P<n>\d+))\b")
CLOSES = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?\s+" + REF, re.I)
PART = re.compile(r"\b(?:part\s+of|partly|partially|towards?)(?:\s+issue)?:?\s+" + REF, re.I)
# Quoted text is not a claim: HTML comments, fenced code and `code spans` are skipped.
QUOTED = re.compile(r"<!--.*?-->|```.*?```|`[^`\n]*`", re.S)


def refs(pattern, body, repo):
    """Issues a pattern names, as lower-case 'owner/repo#n'; a bare #n is in this repo."""
    out = []
    for m in pattern.finditer(QUOTED.sub("", body or "")):
        where = m.group("url_repo") or m.group("repo") or repo
        out.append(f"{where.lower()}#{m.group('url_n') or m.group('n')}")
    return out


def problems(body, repo):
    """What is wrong with a PR body, as sentences. Empty means it passes."""
    closed = refs(CLOSES, body, repo)
    found = []
    if not closed:
        found.append("The PR body closes no issue. Add `Closes #<n>` (or `Closes owner/repo#<n>`) "
                     "for the issue this PR finishes. Every PR closes an issue (design 6.8).")
    for ref in dict.fromkeys(refs(PART, body, repo)):
        if ref not in closed:
            found.append(f"The PR does part of {ref} without closing it. Split the issue first: "
                         "make the part this PR does a sub-issue, and close that (design 6.8).")
    return found


def exempt(author, author_type, sources):
    """True for a PR from a listed bot whose PRs have no issue behind them."""
    return author_type == "Bot" and sources.get(bot_login(author), {}).get("no_issue", False)


GITHUB_COMMITTER = "web-flow"  # GitHub's own committer, for commits it makes through its API or UI


def verified(c):
    return bool(((c.get("commit") or {}).get("verification") or {}).get("verified"))


def login(c, who):
    return bot_login((c.get(who) or {}).get("login"))


def only_bot_commits(commits, author):
    """True if every commit on the PR, GitHub's base-branch merges aside, is the bot's own: authored
    by it, committed by it or by GitHub, and verified."""
    bot = bot_login(author)
    own = [c for c in commits if not (len(c.get("parents") or []) > 1 and verified(c)
                                      and login(c, "committer") == GITHUB_COMMITTER)]
    return bool(own) and all(login(c, "author") == bot and login(c, "committer") in (bot, GITHUB_COMMITTER)
                             and verified(c) for c in own)


def pr_commits(repo, number):
    """The PR's commits from the REST API, or None if they can't be read."""
    run = subprocess.run([os.environ.get("GH", "gh"), "api", "--paginate", "--slurp",
                          f"repos/{repo}/pulls/{number}/commits?per_page=100"], capture_output=True, text=True)
    if run.returncode != 0:
        print(f"::error::couldn't read the PR's commits ({run.stderr.strip()}): re-run the job")
        return None
    return [c for page in json.loads(run.stdout) for c in page]


def main():
    author = os.environ.get("PR_AUTHOR", "")
    body, repo = os.environ.get("PR_BODY", ""), os.environ["GITHUB_REPOSITORY"]
    if exempt(author, os.environ.get("PR_AUTHOR_TYPE", ""), bot_sources()):
        commits = pr_commits(repo, os.environ.get("PR_NUMBER", ""))
        if commits is None:
            sys.exit(1)
        if only_bot_commits(commits, author):
            print(f"OK: opened by {author}, a bot with no issue behind its PRs, and every commit is "
                  "its own; its source label says where it came from (rulesets/bots.json)")
            return
        print(f"Opened by {author}, but not every commit is its own and signed, so it needs "
              "`Closes #n` like any PR.")
    found = problems(body, repo)
    for p in found:
        print(f"::error::{p}")
    if found:
        print("Fix the PR body; saving it runs this check again. Re-running the job does not help: "
              "it reads the body the PR had when the run started.")
        sys.exit(1)
    print("OK: the PR closes " + ", ".join(dict.fromkeys(refs(CLOSES, body, repo))))


if __name__ == "__main__":
    main()

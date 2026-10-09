#!/usr/bin/env python3
"""Check that a PR closes at least one issue, and only whole issues (design 6.8, checks 1 and 2).

The PR body must say `Closes #n` (or Fixes / Resolves, any tense, also `owner/repo#n` or an issue
URL) for at least one issue: only those forms make GitHub close the issue on merge, so only they
count. A PR that says it is "part of" (or "partly", "partially", "towards") an issue it doesn't
close fails: that work splits the issue first, and the PR closes the new sub-issue. Quoted text
(HTML comments, code) is not read.

A PR from a bot with no issue behind it (`no_issue` in rulesets/bots.json: Renovate's dependency
updates) passes without one; its source label says where it came from (design 6.8, check 1).

Usage: scripts/pr_closes_issue.py      (reads PR_BODY, PR_AUTHOR, PR_AUTHOR_TYPE and
GITHUB_REPOSITORY from the environment)
In Actions it runs as the `closes-issue` job of the reusable pr-title workflow.
"""

import os
import re
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


def main():
    author = os.environ.get("PR_AUTHOR", "")
    if exempt(author, os.environ.get("PR_AUTHOR_TYPE", ""), bot_sources()):
        print(f"OK: opened by {author}, a bot with no issue behind its PRs; its source label "
              "says where it came from (rulesets/bots.json)")
        return
    body, repo = os.environ.get("PR_BODY", ""), os.environ["GITHUB_REPOSITORY"]
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

#!/usr/bin/env python3
"""Check that a PR is not stacked on another open PR in the same repo (handbook#120): its branch
holds no commit that another open PR's branch also holds and the base branch does not. Every PR is
squash-merged, so when the earlier PR merges, the later one's copies of its commits conflict with
`main` (animations#82 to #84 needed three rounds of conflict fixes).

A PR whose base branch is the other PR's branch is not caught: the shared commits are on its base.

Usage: scripts/pr_not_stacked.py   (reads GITHUB_REPOSITORY, PR_NUMBER, PR_BASE_REF, PR_HEAD_SHA
and GITHUB_TOKEN from the environment; in Actions it runs as the `stacked` job of the reusable
pr-title workflow)
       scripts/pr_not_stacked.py --repo <owner/name> [--pr <n>]   (read-only, through `gh api`:
reports every open PR it would fail, or just PR <n>)

Cost: one compare for this PR, one list of open PRs, and one commit list for each older open PR
whose head is not already among this PR's commits.
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request


def api(token):
    """A GET function for the GitHub REST API: path -> parsed JSON."""
    def get(path):
        request = urllib.request.Request(f"https://api.github.com/{path}",
                                         headers={"Authorization": f"Bearer {token}",
                                                  "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    return get


def gh_api(gh):
    """A GET function through the GitHub CLI, for the read-only report."""
    def get(path):
        return json.loads(subprocess.run([gh, "api", path], capture_output=True, text=True,
                                         check=True).stdout)
    return get


def open_prs(get, repo):
    """Every open PR in the repo."""
    prs, page = [], 1
    while True:
        batch = get(f"repos/{repo}/pulls?state=open&per_page=100&page={page}")
        prs += batch
        if len(batch) < 100:
            return prs
        page += 1


def branch_commits(get, repo, base_ref, head_sha):
    """SHAs of the commits on head that are not on base (the compare API lists up to 250)."""
    base = urllib.parse.quote(base_ref, safe="")
    return {c["sha"] for c in get(f"repos/{repo}/compare/{base}...{head_sha}")["commits"]}


def pr_commits(get, repo, number):
    """SHAs of an open PR's own commits (its first 100, oldest first, where shared ones sit)."""
    return {c["sha"] for c in get(f"repos/{repo}/pulls/{number}/commits?per_page=100")}


def stacked_on(get, repo, number, head_sha, mine, others):
    """The open PRs this one is stacked on, as (number, title, head branch). `mine` is this PR's
    own commits; `others` the repo's open PRs as the API lists them.

    Stacked on X when X's head commit is among this PR's commits (this branch was made from X's).
    When the two only share earlier commits (one branch was made from the middle of the other and
    both moved on), the order can't be read from the commits, so the newer PR is the stacked one.
    A PR stacked on this one (this PR's head is among its commits) does not count against this one.
    """
    found = []
    for other in others:
        if other["number"] == number:
            continue
        hit = other["head"]["sha"] in mine
        if not hit and other["number"] < number:
            theirs = pr_commits(get, repo, other["number"])
            hit = head_sha not in theirs and bool(theirs & mine)
        if hit:
            found.append((other["number"], other["title"], other["head"]["ref"]))
    return found


def problems(found, base_ref):
    """What is wrong, as sentences. Empty means it passes."""
    return [f"This PR is stacked on #{n} ({title!r}, branch {ref}): its branch holds commits from "
            f"that PR's branch that are not on {base_ref}. PRs are squash-merged, so when #{n} "
            f"merges these copies of its commits conflict with {base_ref}. Fix: branch from the "
            f"latest {base_ref} and move only this PR's own commits over (git switch -c <new> "
            f"origin/{base_ref}; git cherry-pick <this PR's commits>), or wait for #{n} to merge, "
            f"then merge {base_ref} into this branch (git merge origin/{base_ref}; for each "
            f"conflict keep {base_ref}'s version of #{n}'s changes)."
            for n, title, ref in found]


def check(get, repo, number, base_ref, head_sha):
    """problems() for one PR."""
    mine = branch_commits(get, repo, base_ref, head_sha)
    return problems(stacked_on(get, repo, number, head_sha, mine, open_prs(get, repo)), base_ref)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", help="owner/name; report its open PRs read-only")
    parser.add_argument("--pr", type=int, help="with --repo: only this PR")
    args = parser.parse_args()

    if args.repo:
        get = gh_api(os.environ.get("GH", "gh"))
        prs = open_prs(get, args.repo)
        for pr in prs:
            if args.pr and pr["number"] != args.pr:
                continue
            found = check(get, args.repo, pr["number"], pr["base"]["ref"], pr["head"]["sha"])
            print(f"#{pr['number']} {pr['title']}: {'FAIL' if found else 'ok'}")
            for p in found:
                print(f"  {p}")
        return

    repo, number, base_ref, head_sha, token = (
        os.environ.get(k) for k in ("GITHUB_REPOSITORY", "PR_NUMBER", "PR_BASE_REF", "PR_HEAD_SHA",
                                    "GITHUB_TOKEN"))
    found = check(api(token), repo, int(number), base_ref, head_sha)
    for p in found:
        print(f"::error::{p}")
    if found:
        sys.exit(1)
    print(f"OK: no commit on this branch is on another open PR's branch (base {base_ref})")


if __name__ == "__main__":
    main()

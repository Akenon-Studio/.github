#!/usr/bin/env python3
"""Check that live GitHub settings match rulesets/ (design 6.7). Exits 1 if anything differs.

Usage: scripts/verify-settings.py
Reports: org settings, each managed repo's merge settings and rulesets, the project board's fields
and linked repos (rulesets/board.json), and any repo in the org that is neither managed nor archived.
"""

import sys

from board import board_differences, find_board, spec
from rules import ORG, desired_rulesets, differences, gh, load, managed_repos, repo_rulesets


def main():
    problems = []

    org = gh(f"orgs/{ORG}")
    settings = load("org-settings.json")
    want = {**settings["apply"], **settings["check_only"]}
    problems += [f"org: {d}" for d in differences(want, org)]

    managed = managed_repos()
    for repo in managed:
        problems += [f"{repo} settings: {d}"
                     for d in differences(load("repo-settings.json"), gh(f"repos/{ORG}/{repo}"))]
        have = repo_rulesets(repo)
        for ruleset in desired_rulesets(repo):
            if ruleset["name"] not in have:
                problems.append(f"{repo}: ruleset '{ruleset['name']}' is missing")
                continue
            problems += [f"{repo} ruleset '{ruleset['name']}': {d}"
                         for d in differences(ruleset, have[ruleset["name"]])]
        for extra in sorted(set(have) - {r["name"] for r in desired_rulesets(repo)}):
            problems.append(f"{repo}: unexpected ruleset '{extra}'")

    board = spec()
    problems += [f"board: {d}" for d in board_differences(find_board(board["title"]), board)]

    for r in gh(f"orgs/{ORG}/repos?per_page=100") or []:
        if r["name"] not in managed and not r["archived"]:
            print(f"note: {r['name']} is not managed by rulesets/repos.txt and not archived")

    if problems:
        print("\nFAIL: live settings differ from rulesets/:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print(f"\nPASS: org settings and {len(managed)} managed repo(s) and the board match rulesets/")


if __name__ == "__main__":
    main()

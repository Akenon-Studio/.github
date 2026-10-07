#!/usr/bin/env python3
"""Apply rulesets/ to GitHub: org settings, then each managed repo's merge settings and rulesets.

Usage: scripts/apply-rules.py [repo ...]     (default: every repo in rulesets/repos.txt)
Needs an org owner logged in to gh with the admin:org scope.
"""

import sys

from rules import ORG, desired_rulesets, gh, load, managed_repos, repo_rulesets


def apply_org():
    gh(f"orgs/{ORG}", "-X", "PATCH", body=load("org-settings.json")["apply"])
    print(f"org {ORG}: settings applied")


def apply_repo(repo):
    gh(f"repos/{ORG}/{repo}", "-X", "PATCH", body=load("repo-settings.json"))
    existing = repo_rulesets(repo)
    for ruleset in desired_rulesets(repo):
        if ruleset["name"] in existing:
            rid = existing[ruleset["name"]]["id"]
            gh(f"repos/{ORG}/{repo}/rulesets/{rid}", "-X", "PUT", body=ruleset)
            print(f"{repo}: ruleset '{ruleset['name']}' updated")
        else:
            gh(f"repos/{ORG}/{repo}/rulesets", "-X", "POST", body=ruleset)
            print(f"{repo}: ruleset '{ruleset['name']}' created")


def main():
    repos = sys.argv[1:] or managed_repos()
    apply_org()
    for repo in repos:
        apply_repo(repo)
    print("Done. Now run scripts/verify-settings.py")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Apply rulesets/ to GitHub: org settings, issue types and teams (rulesets/teams.json), then each
managed repo's merge settings, rulesets and labels.

Usage: scripts/apply-rules.py [repo ...]     (default: every repo in rulesets/repos.txt)
Needs an org owner logged in to gh with the admin:org scope.
"""

import sys
import urllib.parse

from rules import (ISSUE_TYPE_KEYS, ORG, TEAM_KEYS, desired_labels, desired_rulesets, gh, load,
                   managed_repos, repo_rulesets, try_gh)


def apply_org():
    gh(f"orgs/{ORG}", "-X", "PATCH", body=load("org-settings.json")["apply"])
    print(f"org {ORG}: settings applied")
    live = {t["name"]: t for t in gh(f"orgs/{ORG}/issue-types") or []}
    for t in load("issue-types.json")["types"]:
        if t["name"] not in live:
            gh(f"orgs/{ORG}/issue-types", "-X", "POST", body=t)
            print(f"org {ORG}: issue type '{t['name']}' created")
        elif any(live[t["name"]].get(k) != t[k] for k in ISSUE_TYPE_KEYS):
            gh(f"orgs/{ORG}/issue-types/{live[t['name']]['id']}", "-X", "PUT", body=t)
            print(f"org {ORG}: issue type '{t['name']}' updated")


def apply_teams():
    """Each team in teams.json: create or update it, set its members to exactly the list, and give
    it its permission on every managed repo."""
    for t in load("teams.json")["teams"]:
        fields = {k: t[k] for k in TEAM_KEYS}
        if try_gh(f"orgs/{ORG}/teams/{t['slug']}") is None:
            gh(f"orgs/{ORG}/teams", "-X", "POST", body=fields)
            print(f"org {ORG}: team '{t['slug']}' created")
        else:
            gh(f"orgs/{ORG}/teams/{t['slug']}", "-X", "PATCH", body=fields)
        wanted = {m.lower(): m for m in t["members"]}
        for login in wanted.values():
            gh(f"orgs/{ORG}/teams/{t['slug']}/memberships/{login}", "-X", "PUT",
               body={"role": "member"})
        for m in gh(f"orgs/{ORG}/teams/{t['slug']}/members?per_page=100") or []:
            if m["login"].lower() not in wanted:  # e.g. the owner who created it
                gh(f"orgs/{ORG}/teams/{t['slug']}/memberships/{m['login']}", "-X", "DELETE")
                print(f"org {ORG}: {m['login']} removed from team '{t['slug']}'")
        for repo in managed_repos():
            gh(f"orgs/{ORG}/teams/{t['slug']}/repos/{ORG}/{repo}", "-X", "PUT",
               body={"permission": t["repo_permission"]})
        print(f"org {ORG}: team '{t['slug']}' applied")


def apply_labels(repo):
    live = {l["name"].lower() for l in gh(f"repos/{ORG}/{repo}/labels?per_page=100") or []}
    for label in desired_labels(repo):
        if label["name"].lower() in live:
            name = urllib.parse.quote(label["name"], safe="")
            gh(f"repos/{ORG}/{repo}/labels/{name}", "-X", "PATCH",
               body={"new_name": label["name"], "color": label["color"],
                     "description": label["description"]})
        else:
            gh(f"repos/{ORG}/{repo}/labels", "-X", "POST", body=label)
    print(f"{repo}: labels applied")


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
    apply_labels(repo)


def main():
    repos = sys.argv[1:] or managed_repos()
    apply_org()
    apply_teams()
    for repo in repos:
        apply_repo(repo)
    print("Done. Now run scripts/verify-settings.py")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Apply rulesets/ to GitHub: the org's settings (rulesets/org-settings.json) and issue types, its
teams (rulesets/teams.json) and its rulesets (rulesets/org-rulesets.json), then each managed repo's merge settings, rulesets and labels.

Usage: scripts/apply-rules.py [repo ...]     (default: every repo in rulesets/repos.txt)
       scripts/apply-rules.py --plan-org-rulesets
                                             change nothing: print what applying would do to each
                                             org ruleset (create, update with each difference, or
                                             nothing), and any org ruleset not in the file
Needs an org owner logged in to gh with the admin:org scope (reads only, for --plan-org-rulesets).
"""

import sys
import urllib.parse

from rules import (FORK_APPROVAL, FORK_APPROVAL_ENDPOINT, ISSUE_TYPE_KEYS, ORG,
                   REPO_SETTING_ENDPOINTS, TEAM_KEYS, desired_labels, desired_org_rulesets,
                   desired_rulesets, gh, is_public, load, managed_repos, org_roles,
                   org_ruleset_differences, org_rulesets, repo_rulesets, team_org_roles, team_repos,
                   try_gh)


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


def apply_org_rulesets():
    existing = org_rulesets()
    for ruleset in desired_org_rulesets():
        if ruleset["name"] in existing:
            gh(f"orgs/{ORG}/rulesets/{existing[ruleset['name']]['id']}", "-X", "PUT", body=ruleset)
            print(f"org {ORG}: ruleset '{ruleset['name']}' updated")
        else:
            gh(f"orgs/{ORG}/rulesets", "-X", "POST", body=ruleset)
            print(f"org {ORG}: ruleset '{ruleset['name']}' created")


def plan_org_rulesets():
    """What apply_org_rulesets() would change, changing nothing; returns how many it would."""
    existing = org_rulesets()
    wanted = desired_org_rulesets()
    changes = 0
    for ruleset in wanted:
        if ruleset["name"] not in existing:
            print(f"org {ORG}: ruleset '{ruleset['name']}' would be created")
            changes += 1
            continue
        diffs = org_ruleset_differences(ruleset, existing[ruleset["name"]])
        if diffs:
            changes += 1
            print(f"org {ORG}: ruleset '{ruleset['name']}' would be updated:")
            print("".join(f"  {d}\n" for d in diffs), end="")
        else:
            print(f"org {ORG}: ruleset '{ruleset['name']}' unchanged")
    for extra in sorted(set(existing) - {r["name"] for r in wanted}):
        print(f"org {ORG}: ruleset '{extra}' is not in rulesets/org-rulesets.json; left alone")
    return changes


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
        allowed = set(team_repos(t))
        for repo in managed_repos():
            if repo in allowed:
                gh(f"orgs/{ORG}/teams/{t['slug']}/repos/{ORG}/{repo}", "-X", "PUT",
                   body={"permission": t["repo_permission"]})
            else:  # a team limited to some repos has no access to the others
                try_gh(f"orgs/{ORG}/teams/{t['slug']}/repos/{ORG}/{repo}", "-X", "DELETE")
        print(f"org {ORG}: team '{t['slug']}' applied")
    roles = org_roles()
    if roles is None:
        print(f"note: can't read the org's roles; team org roles not applied")
        return
    have = team_org_roles(roles)
    for t in load("teams.json")["teams"]:
        want = {t["org_role"]} if t.get("org_role") else set()
        live = have.get(t["slug"].lower(), set())
        for name in want - live:
            gh(f"orgs/{ORG}/organization-roles/teams/{t['slug']}/{roles[name]}", "-X", "PUT")
            print(f"org {ORG}: team '{t['slug']}' given org role {name}")
        for name in live - want:
            gh(f"orgs/{ORG}/organization-roles/teams/{t['slug']}/{roles[name]}", "-X", "DELETE")
            print(f"org {ORG}: org role {name} removed from team '{t['slug']}'")


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
    settings = load("repo-settings.json")
    gh(f"repos/{ORG}/{repo}", "-X", "PATCH",
       body={k: v for k, v in settings.items() if k not in REPO_SETTING_ENDPOINTS})
    for key, endpoint in REPO_SETTING_ENDPOINTS.items():
        gh(f"repos/{ORG}/{repo}/{endpoint}", "-X", "PUT" if settings[key] else "DELETE")
    existing = repo_rulesets(repo)
    for ruleset in desired_rulesets(repo):
        if ruleset["name"] in existing:
            rid = existing[ruleset["name"]]["id"]
            gh(f"repos/{ORG}/{repo}/rulesets/{rid}", "-X", "PUT", body=ruleset)
            print(f"{repo}: ruleset '{ruleset['name']}' updated")
        else:
            gh(f"repos/{ORG}/{repo}/rulesets", "-X", "POST", body=ruleset)
            print(f"{repo}: ruleset '{ruleset['name']}' created")
    if is_public(repo):
        gh(f"repos/{ORG}/{repo}/{FORK_APPROVAL_ENDPOINT}", "-X", "PUT",
           body={"approval_policy": FORK_APPROVAL})
    apply_labels(repo)


def main():
    if sys.argv[1:] == ["--plan-org-rulesets"]:
        changes = plan_org_rulesets()
        print(f"Plan: {changes} org ruleset change(s); nothing was applied")
        return
    if any(a.startswith("-") for a in sys.argv[1:]):
        sys.exit(__doc__)
    repos = sys.argv[1:] or managed_repos()
    apply_org()
    apply_teams()  # first: an org ruleset may name a team that is new in teams.json
    apply_org_rulesets()
    for repo in repos:
        apply_repo(repo)
    print("Done. Now run scripts/verify-settings.py")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Check that live GitHub settings match rulesets/ (design 6.7). Exits 1 if anything differs.

Usage: scripts/verify-settings.py
Reports: org settings, issue types and teams (rulesets/teams.json), each managed repo's merge
settings, rulesets and labels, the project board's fields
and linked repos (rulesets/board.json), the files that list every repo and the files every repo needs
(rulesets/repo-lists.json), that every team a repo's CODEOWNERS names is in rulesets/teams.json
with write access, that every repo's caller workflows listen to the same events as this
repo's, and any repo in the org that is neither managed nor archived.
"""

import re
import sys

from board import board_differences, find_board, spec
from rules import (CODEOWNERS_PATHS, ORG, ROOT, codeowners_team_differences, org_role_differences,
                   org_roles, team_org_roles, desired_labels,
                   desired_rulesets, differences, gh, issue_type_differences, label_differences, load,
                   managed_repos, read_file, repo_files, repo_rulesets, repo_settings, team_differences,
                   team_repo_permission, try_gh)


def missing_from(text, repos, pattern, skip=()):
    """Managed repos a repo-list file doesn't name."""
    return [r for r in repos if r not in skip
            and not re.search(pattern.replace("{repo}", re.escape(r)), text, re.MULTILINE)]


CALLERS = (".github/workflows/pr-title-caller.yml", ".github/workflows/issue-fields-caller.yml")
TRIGGERS = re.compile(r"^\s*types:\s*\[(.*?)\]", re.MULTILINE)


def triggers(text):
    """The event types a caller workflow listens to, as a set (empty if it names none)."""
    found = TRIGGERS.search(text or "")
    return {t.strip() for t in found.group(1).split(",")} if found else set()


def main():
    problems = []

    org = gh(f"orgs/{ORG}")
    settings = load("org-settings.json")
    want = {**settings["apply"], **settings["check_only"]}
    problems += [f"org: {d}" for d in differences(want, org)]

    types = try_gh(f"orgs/{ORG}/issue-types")
    if types is None:
        print("note: can't read the org's issue types; not checked")
    else:
        problems += [f"org: {d}" for d in issue_type_differences(load("issue-types.json")["types"], types)]

    teams_readable = try_gh(f"orgs/{ORG}/teams") is not None
    for t in load("teams.json")["teams"]:
        if not teams_readable:
            print(f"note: can't read the org's teams; team '{t['slug']}' not checked")
            continue
        members = try_gh(f"orgs/{ORG}/teams/{t['slug']}/members?per_page=100")
        if members is None:
            problems.append(f"org: team '{t['slug']}' is missing")
            continue
        repos = {r["name"]: team_repo_permission(r.get("permissions") or {})
                 for r in gh(f"orgs/{ORG}/teams/{t['slug']}/repos?per_page=100") or []}
        problems += [f"org: {d}" for d in team_differences(
            t, gh(f"orgs/{ORG}/teams/{t['slug']}"), [m["login"] for m in members], repos)]

    roles = org_roles() if teams_readable else None
    if roles is None:
        print("note: can't read the org's roles; team org roles not checked")
    else:
        problems += [f"org: {d}" for d in org_role_differences(load("teams.json")["teams"],
                                                                team_org_roles(roles))]

    managed = managed_repos()
    for repo in managed:
        problems += [f"{repo} settings: {d}"
                     for d in differences(load("repo-settings.json"), repo_settings(repo))]
        have = repo_rulesets(repo)
        for ruleset in desired_rulesets(repo):
            if ruleset["name"] not in have:
                problems.append(f"{repo}: ruleset '{ruleset['name']}' is missing")
                continue
            problems += [f"{repo} ruleset '{ruleset['name']}': {d}"
                         for d in differences(ruleset, have[ruleset["name"]])]
        for extra in sorted(set(have) - {r["name"] for r in desired_rulesets(repo)}):
            problems.append(f"{repo}: unexpected ruleset '{extra}'")
        labels = try_gh(f"repos/{ORG}/{repo}/labels?per_page=100")
        if labels is None:
            print(f"note: can't read {repo}'s labels; not checked")
        else:
            problems += [f"{repo}: {d}" for d in label_differences(desired_labels(repo), labels)]

    board = spec()
    problems += [f"board: {d}" for d in board_differences(find_board(board["title"]), board)]

    for spec_ in load("repo-lists.json")["lists"]:
        text = read_file(spec_["repo"], spec_["path"])
        if text is None:
            print(f"note: can't read {spec_['repo']}/{spec_['path']}; repo list not checked")
            continue
        problems += [f"{spec_['repo']}/{spec_['path']}: doesn't list repo '{r}'"
                     for r in missing_from(text, managed, spec_["pattern"], spec_["skip"])]

    for repo in managed:
        files = repo_files(repo)
        if files is None:
            print(f"note: can't read {repo}'s files; required files not checked")
            continue
        problems += [f"{repo}: missing {f}" for f in load("repo-lists.json")["required_files"] if f not in files]
        owners = next((p for p in CODEOWNERS_PATHS if p in files), None)
        text = read_file(repo, owners) if owners else None
        if text is not None:
            problems += [f"{repo}/{owners}: {d}"
                         for d in codeowners_team_differences(text, load("teams.json")["teams"], repo)]

    for path in CALLERS:
        want = triggers((ROOT / path).read_text())
        for repo in managed:
            text = read_file(repo, path)
            if text is None:
                print(f"note: can't read {repo}/{path}; its triggers not checked")
            elif triggers(text) != want:
                problems.append(f"{repo}/{path}: listens to {sorted(triggers(text))}, expected "
                                f"{sorted(want)} (copy this repo's)")

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

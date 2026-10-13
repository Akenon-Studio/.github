#!/usr/bin/env python3
"""Check that live GitHub settings match rulesets/ (design 6.7). Exits 1 if anything differs.

Usage: scripts/verify-settings.py
Reports: org settings and rulesets (rulesets/org-rulesets.json), issue types and teams (rulesets/teams.json), each managed repo's merge
settings, rulesets and labels, the project board's fields
and linked repos (rulesets/board.json), the files that list every repo and the files every repo needs
(rulesets/repo-lists.json), that every team a repo's CODEOWNERS names is in rulesets/teams.json
with write access, that each CODEOWNERS ends with its generated sensitive-areas block and every
sensitive path exists (rulesets/sensitive-paths.json), a public repo's fork PR workflow approval,
that every repo's caller workflows listen to the same events as this
repo's, and any repo in the org that is neither managed nor archived.
"""

import re
import sys

from board import board_differences, find_board, spec
import sensitive_paths
from rules import (CODEOWNERS_PATHS, FORK_APPROVAL, FORK_APPROVAL_ENDPOINT, ORG, ROOT, is_public,
                   codeowners_team_differences, org_role_differences,
                   org_roles, team_org_roles, desired_labels,
                   desired_org_rulesets, desired_rulesets, differences, gh, issue_type_differences, label_differences, load,
                   managed_repos, read_file, repo_files, repo_rulesets, repo_settings, team_differences,
                   team_repo_permission, try_gh)


def missing_from(text, repos, pattern, skip=()):
    """Managed repos a repo-list file doesn't name."""
    return [r for r in repos if r not in skip
            and not re.search(pattern.replace("{repo}", re.escape(r)), text, re.MULTILINE)]


CALLERS = (".github/workflows/pr-title-caller.yml", ".github/workflows/issue-fields-caller.yml")
# An event and its types list, with comment lines between allowed
TRIGGERS = re.compile(r"^\s*([a-z_]+):[ \t]*(?:#.*)?\n(?:[ \t]*#.*\n)*[ \t]*types:\s*\[(.*?)\]", re.MULTILINE)


def triggers(text):
    """The event types a caller workflow listens to, as a set of `event: type` (empty if it names
    none), so a repo missing a whole event shows too."""
    return {f"{event}: {t.strip()}" for event, types in TRIGGERS.findall(text or "")
            for t in types.split(",")}


def main():
    problems = []

    org = gh(f"orgs/{ORG}")
    settings = load("org-settings.json")
    want = {**settings["apply"], **settings["check_only"]}
    problems += [f"org: {d}" for d in differences(want, org)]

    listed = try_gh(f"orgs/{ORG}/rulesets")
    if listed is None:
        print("note: can't read the org's rulesets; not checked")
    else:
        have = {r["name"]: try_gh(f"orgs/{ORG}/rulesets/{r['id']}") for r in listed}
        for ruleset in desired_org_rulesets():
            if ruleset["name"] not in have:
                problems.append(f"org: ruleset '{ruleset['name']}' is missing")
            else:
                problems += [f"org ruleset '{ruleset['name']}': {d}"
                             for d in differences(ruleset, have[ruleset["name"]])]
        for extra in sorted(set(have) - {r["name"] for r in desired_org_rulesets()}):
            print(f"note: org ruleset '{extra}' is not in rulesets/org-rulesets.json")

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
        if is_public(repo):
            fork = try_gh(f"repos/{ORG}/{repo}/{FORK_APPROVAL_ENDPOINT}") or {}
            policy = fork.get("approval_policy")
            if policy is None:
                print(f"note: can't read {repo}'s fork PR workflow approval; not checked")
            elif policy != FORK_APPROVAL:
                problems.append(f"{repo} settings: fork PR workflow approval is {policy}, want "
                                f"{FORK_APPROVAL} (design 6.2)")
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
            problems += [f"{repo}/{owners}: {d}" for d in sensitive_paths.codeowners_problems(repo, text)]
        elif owners is None and sensitive_paths.block(repo):
            problems.append(f"{repo}: no CODEOWNERS, but rulesets/sensitive-paths.json gives it a sign-off")
        elif owners:
            print(f"note: can't read {repo}/{owners}; its sensitive-areas block not checked")
        problems += [f"{repo}: rulesets/sensitive-paths.json: {d}"
                     for d in sensitive_paths.path_problems(repo, files)]

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

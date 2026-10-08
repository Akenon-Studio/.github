#!/usr/bin/env python3
"""Check that live GitHub settings match rulesets/ (design 6.7). Exits 1 if anything differs.

Usage: scripts/verify-settings.py
Reports: org settings and issue types, each managed repo's merge settings, rulesets and labels,
the project board's fields
and linked repos (rulesets/board.json), the files that list every repo and the files every repo needs
(rulesets/repo-lists.json), that every repo's caller workflows listen to the same events as this
repo's, and any repo in the org that is neither managed nor archived.
"""

import base64
import json
import os
import re
import subprocess
import sys

from board import board_differences, find_board, spec
from rules import (ORG, ROOT, desired_labels, desired_rulesets, differences, gh, issue_type_differences,
                   label_differences, load, managed_repos, repo_rulesets, repo_settings, try_gh)


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


def read_file(repo, path):
    """A file's text from a repo's default branch, or None if this token can't read it (the daily
    run's read-only app has no contents access, so that run skips these checks)."""
    res = subprocess.run([os.environ.get("GH", "gh"), "api", f"repos/{ORG}/{repo}/contents/{path}"],
                         capture_output=True, text=True)
    if res.returncode != 0:
        return None
    return base64.b64decode(json.loads(res.stdout)["content"]).decode()


def repo_files(repo):
    """Every file path on a repo's default branch, or None if this token can't read it."""
    res = subprocess.run([os.environ.get("GH", "gh"), "api", f"repos/{ORG}/{repo}/git/trees/HEAD?recursive=1"],
                         capture_output=True, text=True)
    if res.returncode != 0:
        return None
    return {t["path"] for t in json.loads(res.stdout)["tree"]}


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

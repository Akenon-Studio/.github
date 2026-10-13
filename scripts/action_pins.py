#!/usr/bin/env python3
"""The action-pin check (design 6.4): every action commit a PR adds or changes is on that action's
default branch, or on a branch listed for that action in rulesets/action-branches.json.

A pin to a commit has no release date, so Renovate's wait counts it as old enough, and a tag moved
to a malicious commit (the tj-actions attack) would pass the wait anyway. A commit pushed only to a
tag, or only to a fork (GitHub resolves fork-network commits), is on no branch of the action, so
this check fails it. A commit it can't look up fails it too.

It reads the `uses: owner/repo[/path]@<ref>` lines the PR adds in YAML files (workflows, composite
actions). The ref must be a full commit: a tag or branch name fails. Local (`./`) and `docker://`
actions and the org's own reusable workflows (Akenon-Studio/..., called by branch) are left alone.
"On a branch" means GitHub's compare API says the commit is the branch's tip or behind it.

It runs from `.github`'s main, never the PR's copy, so a PR can't list a branch for itself.

rulesets/action-branches.json, when it exists: {"actions": {"owner/repo": ["releases/v4", ...]}},
added to only by a reviewed PR a partner approves (design 6.4).

Usage: scripts/action_pins.py <base sha> [repo dir]   (diffs <base>...HEAD; reads GITHUB_TOKEN, or
falls back to `gh api` when it isn't set)
In Actions it runs as the `action-pins` step of the reusable pr-title workflow.
"""

import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.error
import urllib.parse

from pr_not_stacked import api, gh_api

BRANCHES_FILE = pathlib.Path(__file__).resolve().parent.parent / "rulesets" / "action-branches.json"
YAML = re.compile(r"\.ya?ml$")
USES = re.compile(r"""\buses:\s*['"]?([A-Za-z0-9][A-Za-z0-9_-]*/[A-Za-z0-9_.-]+)(?:/[^@\s'"]*)?@([^\s'"#,}]+)""")
COMMIT = re.compile(r"[0-9a-f]{40}")
ORG = "akenon-studio"
ON_BRANCH = {"behind", "identical"}


def added_pins(diff):
    """{(owner/repo lowercased, ref): [file:line, ...]} for the `uses:` of actions outside the org
    that the diff adds, from `git diff --unified=0` output. A commit ref is lowercased."""
    pins, path, line = {}, None, 0
    for text in diff.splitlines():
        if text.startswith("+++ "):
            path = text[6:] if text.startswith("+++ b/") else None
        elif text.startswith("@@"):
            line = int(re.match(r"@@ -\S+ \+(\d+)", text).group(1))
        elif text.startswith("+") and path:
            if YAML.search(path):
                for m in USES.finditer(text):
                    repo, ref = m.group(1).lower(), m.group(2)
                    if repo.split("/")[0] != ORG:
                        ref = ref.lower() if COMMIT.fullmatch(ref.lower()) else ref
                        pins.setdefault((repo, ref), []).append(f"{path}:{line}")
            line += 1
    return pins


def allowed_branches(path=BRANCHES_FILE):
    """{owner/repo lowercased: [branch, ...]} from rulesets/action-branches.json, {} without it."""
    if not path.exists():
        return {}
    return {k.lower(): v for k, v in json.loads(path.read_text()).get("actions", {}).items()}


def on_branch(get, repo, branch, sha):
    """Whether the commit is the branch's tip or behind it."""
    ref = urllib.parse.quote(branch, safe="")
    return get(f"repos/{repo}/compare/{ref}...{sha}").get("status") in ON_BRANCH


def check(get, pins, extra):
    """[(pin, where, problem or None, branch it was found on)] for each pin."""
    results, defaults = [], {}
    for (repo, sha), where in sorted(pins.items()):
        if not COMMIT.fullmatch(sha):
            results.append(((repo, sha), where, "a tag or branch name, which can be moved: pin a full commit "
                            "(Renovate's pin PR does it)", None))
            continue
        try:
            if repo not in defaults:
                defaults[repo] = get(f"repos/{repo}")["default_branch"]
            found = next((b for b in [defaults[repo], *extra.get(repo, [])] if on_branch(get, repo, b, sha)),
                         None)
            problem = None if found else (
                f"not on {repo}'s default branch ({defaults[repo]})"
                + (f" or its listed branches ({', '.join(extra[repo])})" if extra.get(repo) else "")
                + ": a commit pushed only to a tag or a fork. Pin a commit on the default branch, or list "
                  "the action's release branch in .github's rulesets/action-branches.json by a reviewed PR")
        except (urllib.error.HTTPError, urllib.error.URLError, subprocess.CalledProcessError, KeyError) as e:
            found, problem = None, f"couldn't look it up ({e}), so it can't be checked"
        results.append(((repo, sha), where, problem, found))
    return results


def main(argv):
    if not argv or argv[0].startswith("-"):
        print(__doc__)
        return 2
    base, root = argv[0], argv[1] if len(argv) > 1 else "."
    diff = subprocess.run(["git", "diff", "--unified=0", "--no-renames", f"{base}...HEAD"], cwd=root,
                          capture_output=True, text=True, check=True).stdout
    pins = added_pins(diff)
    if not pins:
        print("No action commit added or changed.")
        return 0
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    get = api(token) if token else gh_api(os.environ.get("GH", "gh"))
    failed = 0
    for (repo, sha), where, problem, found in check(get, pins, allowed_branches()):
        if problem:
            failed += 1
            for w in where:
                file, line = w.rsplit(":", 1)
                print(f"::error file={file},line={line}::{repo}@{sha}: {problem}")
        else:
            print(f"OK {repo}@{sha[:12]}: on {found} ({', '.join(where)})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

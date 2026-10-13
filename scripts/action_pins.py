#!/usr/bin/env python3
"""The action-pin check (design 6.4): every action a PR adds or changes is pinned to a full commit on
that action's default branch, or on a branch listed for that action in rulesets/action-branches.json.

A pin to a commit has no release date, so Renovate's wait counts it as old enough, and a tag moved
to a malicious commit (the tj-actions attack) would pass the wait anyway. A commit pushed only to a
tag, or only to a fork (GitHub resolves fork-network commits), is on no branch of the action, so
this check fails it. A commit it can't look up fails it too.

It parses each changed workflow (`.github/workflows/*.yml`) and composite action (`action.yml`) at
the PR's head and its base, and checks every `uses:` value that is new at the head, wherever YAML
puts it (quoted key, folded value, anchor). A value must read as `owner/repo[/path]@<ref>`, and only
a full 40-hex commit is ever looked up; a tag or branch name fails. Local (`./`) actions,
`docker://` images and the org's own reusable workflows (Akenon-Studio/..., called by branch) are
left alone. "On a branch" means the branch's tip, read by name from the branches API (so a tag of
the same name can't stand in), is the commit or a descendant of it.

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

import yaml

from pr_not_stacked import api, gh_api

BRANCHES_FILE = pathlib.Path(__file__).resolve().parent.parent / "rulesets" / "action-branches.json"
CHECKED = re.compile(r"(^|/)\.github/workflows/[^/]+\.ya?ml$|(^|/)action\.ya?ml$")
REF = re.compile(r"([A-Za-z0-9][A-Za-z0-9_-]*/[A-Za-z0-9_.-]+)(/[^@\s]*)?@([^\s@]+)")
COMMIT = re.compile(r"[0-9a-f]{40}")
SHORT_COMMIT = re.compile(r"[0-9a-f]{7,39}")
ON_BRANCH = {"behind", "identical"}
ORG = "akenon-studio"


def uses_values(text):
    """{value: first line} for every `uses` key in a YAML document, wherever it sits."""
    found, seen = {}, set()

    def walk(node):
        if id(node) in seen:
            return
        seen.add(id(node))
        if isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                if isinstance(key, yaml.ScalarNode) and key.value == "uses" and isinstance(value, yaml.ScalarNode):
                    found.setdefault(value.value.strip(), value.start_mark.line + 1)
                walk(key)
                walk(value)
        elif isinstance(node, yaml.SequenceNode):
            for item in node.value:
                walk(item)

    for doc in yaml.compose_all(text, Loader=yaml.SafeLoader):
        walk(doc)
    return found


def git(root, *args):
    return subprocess.run(["git", "-c", "core.quotePath=false", *args], cwd=root, capture_output=True,
                          check=True).stdout


def changed_files(root, base):
    """Workflows and composite actions the PR adds or changes."""
    out = git(root, "diff", "--name-only", "-z", "--no-renames", "--diff-filter=AM", f"{base}...HEAD")
    return [f for f in out.decode().split("\0") if f and CHECKED.search(f)]


def new_uses(root, base):
    """{value: [file:line, ...]} for the `uses:` values new at the head, and [problems] for files
    that can't be read as YAML."""
    merge_base = git(root, "merge-base", base, "HEAD").decode().strip()
    added, problems = {}, []
    for path in changed_files(root, base):
        try:
            head = uses_values(git(root, "show", f"HEAD:{path}").decode())
        except yaml.YAMLError as e:
            problems.append((path, f"couldn't be read as YAML ({e}), so its actions can't be checked"))
            continue
        try:
            before = uses_values(git(root, "show", f"{merge_base}:{path}").decode())
        except (subprocess.CalledProcessError, yaml.YAMLError):
            before = {}
        for value, line in head.items():
            if value not in before:
                added.setdefault(value, []).append(f"{path}:{line}")
    return added, problems


def classify(value):
    """(owner/repo lowercased, commit) to look up; or (None, problem); or (None, None) to skip."""
    if value.startswith(("./", "docker://")):
        return None, None
    m = REF.fullmatch(value)
    if not m:
        return None, "not an `owner/repo[/path]@ref` the check can read"
    repo, ref = m.group(1).lower(), m.group(3)
    if repo.split("/")[0] == ORG:
        return None, None
    if COMMIT.fullmatch(ref.lower()):
        return (repo, ref.lower()), None
    if SHORT_COMMIT.fullmatch(ref.lower()):
        return None, "a short commit: pin the full 40-character commit"
    return None, "a tag or branch name, which can be moved: pin a full commit (Renovate's pin PR does it)"


def allowed_branches(path=BRANCHES_FILE):
    """{owner/repo lowercased: [branch, ...]} from rulesets/action-branches.json, {} without it."""
    if not path.exists():
        return {}
    return {k.lower(): v for k, v in json.loads(path.read_text()).get("actions", {}).items()}


def on_branch(get, repo, branch, sha):
    """Whether the branch's tip is the commit or a descendant of it. The tip is read by branch name,
    so a tag with the same name can't stand in for the branch."""
    tip = get(f"repos/{repo}/branches/{urllib.parse.quote(branch, safe='')}")["commit"]["sha"]
    return get(f"repos/{repo}/compare/{tip}...{sha}").get("status") in ON_BRANCH


def check(get, pins, extra):
    """[((repo, sha), where, problem or None, branch it was found on)] for each (repo, sha) pin."""
    results, defaults = [], {}
    for (repo, sha), where in sorted(pins.items()):
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
        except (urllib.error.HTTPError, urllib.error.URLError, subprocess.CalledProcessError, KeyError,
                TypeError) as e:
            found, problem = None, f"couldn't look it up ({e}), so it can't be checked"
        results.append(((repo, sha), where, problem, found))
    return results


def run(root, base, get, extra):
    """Every line to print, and whether the check passes."""
    added, problems = new_uses(root, base)
    lines = [f"::error file={path}::{problem}" for path, problem in problems]
    pins = {}
    for value, where in sorted(added.items()):
        pin, problem = classify(value)
        if problem:
            lines += [f"::error file={w.rsplit(':', 1)[0]},line={w.rsplit(':', 1)[1]}::{value}: {problem}"
                      for w in where]
        elif pin:
            pins.setdefault(pin, []).extend(where)
    for (repo, sha), where, problem, found in check(get, pins, extra) if pins else []:
        if problem:
            lines += [f"::error file={w.rsplit(':', 1)[0]},line={w.rsplit(':', 1)[1]}::{repo}@{sha}: {problem}"
                      for w in where]
        else:
            lines.append(f"OK {repo}@{sha[:12]}: on {found} ({', '.join(where)})")
    failed = any(line.startswith("::error") for line in lines)
    if not lines:
        lines = ["No outside action added or changed."]
    return lines, not failed


def main(argv):
    if not argv or argv[0].startswith("-"):
        print(__doc__)
        return 2
    base, root = argv[0], argv[1] if len(argv) > 1 else "."
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    get = api(token) if token else gh_api(os.environ.get("GH", "gh"))
    lines, ok = run(root, base, get, allowed_branches())
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

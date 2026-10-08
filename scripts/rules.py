"""Shared code for apply-rules.py and verify-settings.py. Talks to GitHub through the gh CLI."""

import json
import os
import pathlib
import subprocess
import sys

ORG = "akenon-studio"
# Set GH=/path/to/gh if another program called gh comes first on your PATH.
ACTIONS_APP_ID = 15368  # GitHub Actions: only Actions runs can satisfy a required check
ROOT = pathlib.Path(__file__).resolve().parent.parent
RULES = ROOT / "rulesets"


def gh(*args, body=None):
    """Call `gh api`; return parsed JSON (or None for empty responses)."""
    cmd = [os.environ.get("GH", "gh"), "api", *args]
    if body is not None:
        cmd += ["--input", "-"]
    res = subprocess.run(cmd, input=json.dumps(body) if body is not None else None,
                         capture_output=True, text=True)
    if res.returncode != 0:
        sys.exit(f"gh api {' '.join(args)} failed:\n{res.stderr.strip() or res.stdout.strip()}")
    return json.loads(res.stdout) if res.stdout.strip() else None


def try_gh(*args):
    """Like gh() for reads, but None instead of exiting when this token can't read it."""
    res = subprocess.run([os.environ.get("GH", "gh"), "api", *args], capture_output=True, text=True)
    return json.loads(res.stdout) if res.returncode == 0 and res.stdout.strip() else None


def load(name):
    return json.loads((RULES / name).read_text())


def managed_repos():
    lines = (RULES / "repos.txt").read_text().splitlines()
    return [l.strip() for l in lines if l.strip() and not l.startswith("#")]


def required_checks(repo):
    checks = load("required-checks.json")
    names = checks.get("all", []) + checks.get(repo, [])
    return [{"context": n, "integration_id": ACTIONS_APP_ID} for n in names]


def desired_rulesets(repo):
    """The rulesets a repo should have, with its required checks filled in."""
    main = load("main.json")
    for rule in main["rules"]:
        if rule["type"] == "required_status_checks":
            rule["parameters"]["required_status_checks"] = required_checks(repo)
    return [main, load("release-tags.json")]


def repo_rulesets(repo):
    """The repo's own rulesets (not inherited ones), full detail, keyed by name."""
    out = {}
    for r in gh(f"repos/{ORG}/{repo}/rulesets?includes_parents=false") or []:
        out[r["name"]] = gh(f"repos/{ORG}/{repo}/rulesets/{r['id']}")
    return out


# GraphQL names for the settings in repo-settings.json. Read through GraphQL because the REST API
# hides merge settings from read-only callers such as the daily check's app.
REPO_SETTINGS_GRAPHQL = {
    "allow_squash_merge": "squashMergeAllowed",
    "allow_merge_commit": "mergeCommitAllowed",
    "allow_rebase_merge": "rebaseMergeAllowed",
    "allow_auto_merge": "autoMergeAllowed",
    "allow_update_branch": "allowUpdateBranch",
    "delete_branch_on_merge": "deleteBranchOnMerge",
    "squash_merge_commit_title": "squashMergeCommitTitle",
    "squash_merge_commit_message": "squashMergeCommitMessage",
    "has_wiki": "hasWikiEnabled",
}


def repo_settings(repo):
    """A repo's settings from repo-settings.json, keyed by their REST names."""
    fields = " ".join(REPO_SETTINGS_GRAPHQL.values())
    query = f'query($o: String!, $n: String!) {{ repository(owner: $o, name: $n) {{ {fields} }} }}'
    res = gh("graphql", body={"query": query, "variables": {"o": ORG, "n": repo}})
    live = res["data"]["repository"]
    return {rest: live[name] for rest, name in REPO_SETTINGS_GRAPHQL.items()}


def differences(want, have, path=""):
    """Every place `have` falls short of `want`. Extra keys GitHub adds in `have` are ignored."""
    diffs = []
    if isinstance(want, dict):
        if not isinstance(have, dict):
            return [f"{path or '/'}: expected an object, got {have!r}"]
        for k, v in want.items():
            if k.startswith("_"):
                continue
            diffs += differences(v, have.get(k), f"{path}/{k}")
    elif isinstance(want, list) and want and isinstance(want[0], dict) and "type" in want[0]:
        by_type = {r.get("type"): r for r in (have or [])}
        for r in want:
            diffs += differences(r, by_type.get(r["type"]), f"{path}[{r['type']}]")
        extra = set(by_type) - {r["type"] for r in want}
        diffs += [f"{path}: unexpected rule '{t}'" for t in sorted(extra)]
    elif isinstance(want, list):
        norm = lambda xs: sorted(json.dumps(x, sort_keys=True) for x in (xs or []))
        if norm(want) != norm(have):
            diffs.append(f"{path}: expected {want!r}, got {have!r}")
    elif want != have:
        diffs.append(f"{path}: expected {want!r}, got {have!r}")
    return diffs


def desired_labels(repo):
    """The labels a repo should have (rulesets/labels.json): every repo's, then its own."""
    labels = load("labels.json")
    return labels["all"] + labels.get(repo, [])


def ensure_label(full_repo, name):
    """Create or update one label from rulesets/labels.json in 'owner/repo', for automation that
    adds it before apply-rules.py has run there. gh label create --force is quiet if it exists."""
    label = next(l for l in desired_labels(full_repo.split("/")[1]) if l["name"] == name)
    subprocess.run([os.environ.get("GH", "gh"), "label", "create", name, "-R", full_repo, "--force",
                    "--color", label["color"], "--description", label["description"]],
                   check=True, capture_output=True)


def label_differences(want, have):
    """Labels missing from `have` or differing in colour or description. Names and colours
    compare without case, as GitHub treats them."""
    live = {l["name"].lower(): l for l in have}
    diffs = []
    for l in want:
        got = live.get(l["name"].lower())
        if got is None:
            diffs.append(f"label '{l['name']}' is missing")
        elif got["color"].lower() != l["color"].lower() or (got["description"] or "") != l["description"]:
            diffs.append(f"label '{l['name']}': expected colour {l['color']} and description "
                         f"{l['description']!r}, got {got['color']} and {got['description']!r}")
    return diffs


ISSUE_TYPE_KEYS = ("description", "color", "is_enabled")


def issue_type_differences(want, have):
    """Org issue types missing from `have` or differing from rulesets/issue-types.json."""
    live = {t["name"]: t for t in have}
    diffs = []
    for t in want:
        got = live.get(t["name"])
        if got is None:
            diffs.append(f"issue type '{t['name']}' is missing")
            continue
        for k in ISSUE_TYPE_KEYS:
            if got.get(k) != t[k]:
                diffs.append(f"issue type '{t['name']}': expected {k} {t[k]!r}, got {got.get(k)!r}")
    diffs += [f"issue type '{n}' is not in issue-types.json"
              for n in sorted(set(live) - {t["name"] for t in want})]
    return diffs

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

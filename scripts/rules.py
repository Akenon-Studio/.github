"""Shared code for apply-rules.py and verify-settings.py. Talks to GitHub through the gh CLI."""

import base64
import json
import os
import pathlib
import re
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


def read_file(repo, path):
    """A file's text from a repo's default branch, or None if this token can't read it (without
    contents access, callers skip what needs it)."""
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


AUTOMATION_OWNERS = "automation-owners"
TEAM_KEYS = ("name", "description", "privacy")


def team(slug):
    """A team's definition from rulesets/teams.json."""
    return next(t for t in load("teams.json")["teams"] if t["slug"] == slug)


def automation_owners():
    """The people the automation hands its work to (design 6.8), as logins. Read from the code, not
    from GitHub, so the automation needs no permission to read teams."""
    return list(team(AUTOMATION_OWNERS)["members"])


def org_roles():
    """{role name: role id} for the org's organisation roles, or None if they can't be read."""
    data = try_gh(f"orgs/{ORG}/organization-roles")
    return None if data is None else {r["name"]: r["id"] for r in data["roles"]}


def team_org_roles(roles):
    """{team slug: set of org role names} as live, from {role name: id}."""
    have = {}
    for name, role_id in roles.items():
        for t in try_gh(f"orgs/{ORG}/organization-roles/{role_id}/teams?per_page=100") or []:
            have.setdefault(t["slug"].lower(), set()).add(name)
    return have


def org_role_differences(teams, have):
    """How the org roles given to teams differ from teams.json (`org_role`, or none)."""
    diffs = []
    for t in teams:
        want = {t["org_role"]} if t.get("org_role") else set()
        live = have.get(t["slug"].lower(), set())
        if live != want:
            diffs.append(f"team '{t['slug']}': expected org role {sorted(want) or 'none'}, "
                         f"got {sorted(live) or 'none'}")
    return diffs


def team_repos(t):
    """The managed repos a team has access to: its `repos` list, or every managed repo."""
    return list(t.get("repos") or managed_repos())


def team_differences(want, have, members, repos):
    """How a live team falls short of its definition. `have` is the team as the API returns it (None
    if missing), `members` its members' logins, `repos` {repo name: permission} for its repos.
    Logins compare without case."""
    if have is None:
        return [f"team '{want['slug']}' is missing"]
    diffs = [f"team '{want['slug']}': expected {k} {want[k]!r}, got {have.get(k)!r}"
             for k in TEAM_KEYS if have.get(k) != want[k]]
    wanted, live = {m.lower(): m for m in want["members"]}, {m.lower(): m for m in members}
    diffs += [f"team '{want['slug']}': {wanted[m]} is not a member"
              for m in sorted(wanted.keys() - live.keys())]
    diffs += [f"team '{want['slug']}': {live[m]} is a member but not in teams.json"
              for m in sorted(live.keys() - wanted.keys())]
    allowed = set(team_repos(want))
    for repo in managed_repos():
        expected = want["repo_permission"] if repo in allowed else None
        if repos.get(repo) != expected:
            diffs.append(f"team '{want['slug']}': expected {expected!r} on {repo}, "
                         f"got {repos.get(repo)!r}")
    return diffs


def team_repo_permission(permissions):
    """The single permission name the teams API gives as `role_name`, or derived from the flags."""
    for name in ("admin", "maintain", "push", "triage", "pull"):
        if permissions.get(name):
            return name
    return None


# GitHub reads a repo's CODEOWNERS from the first of these that exists.
CODEOWNERS_PATHS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
WRITE_PERMISSIONS = ("push", "maintain", "admin")
CODEOWNERS_TEAM = re.compile(r"(?<![\w.-])@([A-Za-z0-9-]+)/([A-Za-z0-9_.-]+)")


def codeowners_teams(text):
    """Slugs of this org's teams that a CODEOWNERS file names (comments ignored), in order."""
    slugs = []
    for line in text.splitlines():
        for org, slug in CODEOWNERS_TEAM.findall(line.split("#", 1)[0]):
            if org.lower() == ORG and slug.lower() not in slugs:
                slugs.append(slug.lower())
    return slugs


def codeowners_team_differences(text, teams, repo=None):
    """Teams a CODEOWNERS file names that teams.json doesn't define, or defines without write
    access. GitHub silently ignores a CODEOWNERS team that can't write, so its reviews never get
    requested (handbook#102). The live team is checked against teams.json by team_differences."""
    defined = {t["slug"].lower(): t for t in teams}
    diffs = []
    for slug in codeowners_teams(text):
        t = defined.get(slug)
        if t is None:
            diffs.append(f"CODEOWNERS names team '{slug}', which is not in rulesets/teams.json")
        elif t["repo_permission"] not in WRITE_PERMISSIONS:
            diffs.append(f"CODEOWNERS names team '{slug}', which teams.json gives "
                         f"{t['repo_permission']!r}; a code owner needs write ('push')")
        elif repo is not None and t.get("repos") and repo not in t["repos"]:
            diffs.append(f"CODEOWNERS names team '{slug}', which teams.json gives no access to "
                         f"{repo}; a code owner needs write ('push') there")
    return diffs

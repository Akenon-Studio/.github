#!/usr/bin/env python3
"""Keep one issue listing what failed in the automation without anyone being told (design 6.8,
"Automation failures reach a person"): one Task issue in this repo with the `automation-failures`
source label, opened, updated or closed like the settings-drift report (scripts/one_issue.py).
It lists:

- **Scheduled workflow runs** whose latest run failed, in every managed repo: GitHub emails one
  person at most.
- **Renovate problems**, which Mend-hosted Renovate shows only on Mend's site and has no API for:
  each repo's dashboard issue (its "Repository problems" section), any config-error issue Renovate
  opened, a repo with no dashboard, and a dashboard whose run-again box is still ticked. The report
  ticks that box on each run, so Renovate runs again and clears it; still ticked a day later means
  Renovate isn't running there.
- **PRs the merge lane took out** with no activity since (design 6.2): the lane's comment is still
  the last word and no commit came after it, LANE_IDLE_DAYS on.
- **Open secret-scanning alerts** (GitHub's secret scanning runs on the public repos).
- Anything the check couldn't read, so a missing permission shows instead of an empty list.

Usage: scripts/automation_failures.py collect <state.json>   as the read-only settings app (Actions:
                                                             read, Secret scanning alerts: read)
       scripts/automation_failures.py report <state.json>    as the automation app: reads Renovate's
                                                             issues and the lane's PRs, ticks the
                                                             dashboards' run-again box, keeps the issue
In Actions both run in the daily settings-check workflow.
"""

import datetime
import json
import re
import sys

from board import graphql
from one_issue import keep
from rules import ORG, gh, load, managed_repos, read_file, try_gh

REPO = f"{ORG}/.github"
LABEL = "automation-failures"
TITLE = "Automation failures"
FAILED = {"failure", "timed_out", "startup_failure"}
LANE_MARKER = "<!-- merge-lane -->"
LANE_IDLE_DAYS = 2
RENOVATE = "renovate[bot]"
RUN_AGAIN = re.compile(r"^(\s*- \[)([ xX])(\] <!-- manual job -->)", re.M)


def renovate_pages():
    """Renovate's status-page titles (rulesets/bots.json): its dashboard and its config-error issue."""
    return load("bots.json")["sources"]["renovate"]["status_pages"]


# --- collect, as the settings app ---------------------------------------------------------------

def failed_schedules(runs):
    """The latest scheduled run of each workflow, from one repo's runs (newest first, as GitHub
    lists them), where it failed."""
    latest = {}
    for run in runs:
        latest.setdefault(run["workflow_id"], run)
    return [{"workflow": r["name"], "conclusion": r["conclusion"], "url": r["html_url"],
             "at": r["created_at"]} for r in latest.values() if r.get("conclusion") in FAILED]


def collect(path):
    state = {"runs": [], "secrets": [], "unread": []}
    for repo in managed_repos():
        found = try_gh(f"repos/{ORG}/{repo}/actions/runs?event=schedule&per_page=100")
        if found is None:
            state["unread"].append(f"scheduled runs in {repo} (the settings app needs Actions: read)")
            continue
        state["runs"] += [{"repo": repo, **r} for r in failed_schedules(found["workflow_runs"])]
    alerts = try_gh("--paginate", "--slurp", f"orgs/{ORG}/secret-scanning/alerts?state=open&per_page=100")
    if alerts is None:
        state["unread"].append("secret-scanning alerts (the settings app needs Secret scanning "
                               "alerts: read)")
    else:
        state["secrets"] = [{"repo": a["repository"]["name"], "type": a.get("secret_type_display_name")
                             or a.get("secret_type"), "url": a["html_url"], "at": a["created_at"]}
                            for page in alerts for a in page]
    with open(path, "w") as f:
        json.dump(state, f, indent=2)
    print(f"{len(state['runs'])} failed schedule(s), {len(state['secrets'])} secret alert(s), "
          f"{len(state['unread'])} unread")


# --- report, as the automation app ---------------------------------------------------------------

def repository_problems(body):
    """The lines of a Renovate dashboard's "Repository problems" section."""
    found = re.search(r"^## Repository problems\s*$(.*?)(?=^## |\Z)", body or "", re.M | re.S)
    if not found:
        return []
    return [line.strip()[2:].strip() for line in found.group(1).splitlines()
            if line.strip().startswith(("- ", "* "))]


def run_again(body):
    """(was the run-again box ticked?, the body with it ticked), or None if the dashboard has none."""
    found = RUN_AGAIN.search(body or "")
    if not found:
        return None
    return found.group(2) != " ", RUN_AGAIN.sub(r"\1x\3", body, count=1)


def renovate_problems(repo, issues, pages):
    """(problems, the dashboard to tick or None) for one repo, from Renovate's open issues there.
    `pages` is renovate_pages(): [dashboard title, config-error title]."""
    dashboard_title, config_title = pages
    mine = [i for i in issues if (i.get("user") or {}).get("login") == RENOVATE
            and "pull_request" not in i]
    problems = [f"{repo}: Renovate's config is broken, so it has stopped here: [{i['title']}]"
                f"({i['html_url']})" for i in mine if i["title"] == config_title]
    dashboard = next((i for i in mine if i["title"] == dashboard_title), None)
    if dashboard is None:
        return problems + [f"{repo}: no Renovate dashboard issue, so Renovate hasn't run here since "
                           "it was turned on, or isn't installed (Mend's site has its logs)"], None
    link = f"[dashboard]({dashboard['html_url']})"
    problems += [f"{repo}: {p} ({link})" for p in repository_problems(dashboard["body"])]
    box = run_again(dashboard["body"])
    if box is None:
        return problems, None
    if box[0]:
        problems.append(f"{repo}: Renovate hasn't run since yesterday's check: its run-again box is "
                        f"still ticked ({link}; Mend's site has its logs)")
        return problems, None
    return problems, (dashboard["number"], box[1])


LANE_PRS = """query($q: String!, $after: String) { search(type: ISSUE, query: $q, first: 50, after: $after) {
  pageInfo { hasNextPage endCursor }
  nodes { ... on PullRequest { number url title repository { name owner { login } }
    labels(first: 20) { nodes { name } }
    comments(last: 1) { nodes { body createdAt } }
    commits(last: 1) { nodes { commit { committedDate } } } } } } }"""


def lane_idle(pr, now, days=LANE_IDLE_DAYS):
    """True for a PR the merge lane took out (its comment is still the last word, and no commit came
    after it) at least `days` ago, and not labelled for the lane again."""
    last = (pr["comments"]["nodes"] or [None])[-1]
    if not last or not last["body"].startswith(LANE_MARKER) or "Taken out of the merge lane" not in last["body"]:
        return False
    if "ready-to-merge" in {l["name"] for l in pr["labels"]["nodes"]}:
        return False
    said = datetime.datetime.fromisoformat(last["createdAt"].replace("Z", "+00:00"))
    commit = (pr["commits"]["nodes"] or [{}])[-1].get("commit") or {}
    pushed = commit.get("committedDate")
    if pushed and datetime.datetime.fromisoformat(pushed.replace("Z", "+00:00")) > said:
        return False
    return now - said >= datetime.timedelta(days=days)


def lane_prs(now, managed):
    found, after = [], None
    while True:
        page = graphql(LANE_PRS, q=f"org:{ORG} is:pr is:open", after=after)["search"]
        found += [p for p in page["nodes"] if p and p["repository"]["owner"]["login"].lower() == ORG
                  and p["repository"]["name"] in managed and lane_idle(p, now)]
        if not page["pageInfo"]["hasNextPage"]:
            return found
        after = page["pageInfo"]["endCursor"]


def body(sections):
    """The issue body (a Task form) for {heading: [line]}, or None if every list is empty."""
    parts = [f"**{name}**\n\n" + "\n".join(f"- {line}" for line in lines)
             for name, lines in sections.items() if lines]
    if not parts:
        return None
    return f"""### What

These failed in the automation, and nothing else tells anyone:

{chr(10).join(parts)}

### Why

Design 6.8: a failure no one is told about waits unseen. Each needs a person to fix it, or to
re-run it once the cause is gone.

### Done when

- Each line above is fixed
- The next daily check finds nothing and closes this issue

### Discipline

Software

### Priority

High

### Links

Design 6.8; `scripts/automation_failures.py`

### Design decisions

_No response_"""


def sections(state, renovate, lane):
    return {
        "Scheduled runs that failed": [f"{r['repo']}: {r['workflow']} ({r['conclusion']}, "
                                       f"[run]({r['url']}), {r['at'][:10]})" for r in state["runs"]],
        "Renovate": renovate,
        "PRs the merge lane took out, untouched since": [
            f"{p['repository']['name']}#{p['number']}: [{p['title']}]({p['url']})" for p in lane],
        "Open secret-scanning alerts": [f"{a['repo']}: {a['type']} ([alert]({a['url']}), "
                                        f"{a['at'][:10]})" for a in state["secrets"]],
        "Couldn't check": state["unread"],
    }


def report(path, now=None):
    state = json.load(open(path))
    now = now or datetime.datetime.now(datetime.timezone.utc)
    managed, pages, renovate, tick = managed_repos(), renovate_pages(), [], []
    for repo in managed:
        if read_file(repo, "renovate.json") is None:
            continue
        issues = try_gh(f"repos/{ORG}/{repo}/issues?creator={RENOVATE}&state=open&per_page=100")
        if issues is None:
            state["unread"].append(f"Renovate's issues in {repo}")
            continue
        problems, dashboard = renovate_problems(repo, issues, pages)
        renovate += problems
        if dashboard:
            tick.append((repo, *dashboard))
    lane = lane_prs(now, set(managed))
    print(keep(REPO, LABEL, TITLE, body(sections(state, renovate, lane)),
               passed="Nothing failed unseen any more. Closing."))
    # After the report, so a failed edit can't stop it: Renovate runs again and unticks the box.
    for repo, number, text in tick:
        gh(f"repos/{ORG}/{repo}/issues/{number}", "-X", "PATCH", body={"body": text})
        print(f"{repo}: ticked the Renovate dashboard's run-again box")


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ("collect", "report"):
        sys.exit(__doc__)
    (collect if sys.argv[1] == "collect" else report)(sys.argv[2])


if __name__ == "__main__":
    main()

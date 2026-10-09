#!/usr/bin/env python3
"""Keep one issue listing Dependabot alerts that have waited too long with no fix PR (design 6.4).

Dependabot only raises alerts; Renovate opens the fix PR. An alert open for more than STALE_DAYS
with no open Renovate PR for its package (usually because no fixed version exists yet) would
otherwise wait unseen in the Security tab, so it goes on the board: one Task issue in this repo,
with the `dependencies` source label (rulesets/bots.json), opened, updated or closed like the
settings-drift report (scripts/one_issue.py).

Usage: scripts/stale_alerts.py collect <alerts.json>   as the read-only settings app (Dependabot
                                                        alerts: read): every open alert, every repo
       scripts/stale_alerts.py report <alerts.json>    as the automation app: checks for Renovate
                                                        PRs, then keeps the issue
In Actions both run in the daily settings-check workflow.
"""

import datetime
import json
import os
import re
import subprocess
import sys

from one_issue import keep
from rules import ORG, managed_repos, try_gh

REPO = f"{ORG}/.github"
LABEL = "dependencies"
TITLE = "Dependabot alerts with no fix PR"
STALE_DAYS = 3


def collect(path):
    alerts, skipped = [], []
    for repo in managed_repos():
        # This endpoint pages by cursor (Link header), which `gh --paginate` follows.
        pages = try_gh("--paginate", "--slurp",
                       f"repos/{ORG}/{repo}/dependabot/alerts?state=open&per_page=100")
        if pages is None:
            skipped.append(repo)
            continue
        for a in (a for page in pages for a in page):
            fixed = (a.get("security_vulnerability") or {}).get("first_patched_version") or {}
            alerts.append({"repo": repo, "package": a["dependency"]["package"]["name"],
                           "severity": a["security_advisory"]["severity"],
                           "summary": a["security_advisory"]["summary"],
                           "created_at": a["created_at"], "url": a["html_url"],
                           "fixed_in": fixed.get("identifier")})
    with open(path, "w") as f:
        json.dump(alerts, f, indent=2)
    print(f"{len(alerts)} open alert(s)")
    if skipped:
        # Fail rather than report: an incomplete list would close the issue for alerts still open.
        sys.exit(f"Could not read Dependabot alerts in {', '.join(skipped)} (alerts off, or the "
                 "settings app lacks Dependabot alerts: read)")


def stale(alerts, now, days=STALE_DAYS):
    """Alerts older than `days`."""
    cutoff = now - datetime.timedelta(days=days)
    return [a for a in alerts
            if datetime.datetime.fromisoformat(a["created_at"].replace("Z", "+00:00")) < cutoff]


def renovate_titles(repo, cache):
    """Titles of Renovate's open PRs in a repo, read once per repo with the org-wide read-only token
    (PR_READ_TOKEN; the issue is written with GH_TOKEN, scoped to .github); None if unreadable."""
    if repo not in cache:
        env = dict(os.environ, GH_TOKEN=os.environ.get("PR_READ_TOKEN") or os.environ.get("GH_TOKEN", ""))
        res = subprocess.run([os.environ.get("GH", "gh"), "api", "--paginate", "--slurp",
                              f"repos/{ORG}/{repo}/pulls?state=open&per_page=100"],
                             capture_output=True, text=True, env=env)
        pages = json.loads(res.stdout) if res.returncode == 0 and res.stdout.strip() else None
        cache[repo] = None if pages is None else [
            p["title"] for page in pages for p in page
            if (p.get("user") or {}).get("login") == "renovate[bot]"]
    return cache[repo]


def fix_pr(alert, cache):
    """True if a Renovate PR in the alert's repo names the package as a whole word in its title,
    False if no fixed version exists yet, None if the PRs couldn't be read (shown as unknown)."""
    if not alert.get("fixed_in"):
        return False  # no fixed version exists, so no update PR can fix it yet
    titles = renovate_titles(alert["repo"], cache)
    if titles is None:
        return None
    word = re.compile(rf"(?<![\w@/.-]){re.escape(alert['package'])}(?![\w/.-])")
    return any(word.search(t) for t in titles)


def plain(text):
    """Third-party advisory text, safe to put in an issue: a code span, so no @mention or link."""
    return "`" + " ".join(text.split()).replace("`", "'") + "`"


def body(waiting):
    rows = "\n".join(
        f"- **{a['severity']}** `{a['package']}` in {a['repo']}: {plain(a['summary'])} "
        f"({'fixed in ' + a['fixed_in'] if a['fixed_in'] else 'no fixed version yet'}; "
        f"[alert]({a['url']}), open since {a['created_at'][:10]}"
        f"{'; could not check for a Renovate PR' if a.get('pr_unknown') else ''})" for a in waiting)
    return f"""### What

These Dependabot alerts have been open more than {STALE_DAYS} days with no open Renovate PR to fix them:

{rows}

### Why

Dependabot only raises alerts and Renovate opens the fix PR (design 6.4). An alert with no PR
(usually no fixed version yet) needs a person: a workaround, an override to a fixed version, a
replacement package, or a recorded reason it doesn't affect us.

### Done when

- Each alert is fixed, or dismissed in the Security tab with the reason
- The next daily check finds none and closes this issue

### Discipline

Software

### Priority

High

### Links

Design 6.4; `scripts/stale_alerts.py`

### Design decisions

_No response_"""


def report(path, now=None):
    cache, waiting = {}, []
    for a in stale(json.load(open(path)), now or datetime.datetime.now(datetime.timezone.utc)):
        pr = fix_pr(a, cache)
        if pr is not True:
            waiting.append({**a, "pr_unknown": pr is None})
    print(keep(REPO, LABEL, TITLE, body(waiting) if waiting else None, title_match=True,
               passed="No alert is waiting without a fix PR any more. Closing."))


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ("collect", "report"):
        sys.exit(__doc__)
    (collect if sys.argv[1] == "collect" else report)(sys.argv[2])


if __name__ == "__main__":
    main()

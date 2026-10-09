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
import sys
import urllib.parse

from one_issue import keep
from rules import ORG, gh, managed_repos

REPO = f"{ORG}/.github"
LABEL = "dependencies"
TITLE = "Dependabot alerts with no fix PR"
STALE_DAYS = 3


def collect(path):
    alerts = []
    for repo in managed_repos():
        for a in gh(f"repos/{ORG}/{repo}/dependabot/alerts?state=open&per_page=100") or []:
            fixed = (a.get("security_vulnerability") or {}).get("first_patched_version") or {}
            alerts.append({"repo": repo, "package": a["dependency"]["package"]["name"],
                           "severity": a["security_advisory"]["severity"],
                           "summary": a["security_advisory"]["summary"],
                           "created_at": a["created_at"], "url": a["html_url"],
                           "fixed_in": fixed.get("identifier")})
    with open(path, "w") as f:
        json.dump(alerts, f, indent=2)
    print(f"{len(alerts)} open alert(s)")


def stale(alerts, now, days=STALE_DAYS):
    """Alerts older than `days`."""
    cutoff = now - datetime.timedelta(days=days)
    return [a for a in alerts
            if datetime.datetime.fromisoformat(a["created_at"].replace("Z", "+00:00")) < cutoff]


def has_fix_pr(alert):
    """True if Renovate has an open PR in the alert's repo that names the package."""
    q = f'repo:{ORG}/{alert["repo"]} is:pr is:open author:app/renovate "{alert["package"]}"'
    found = gh(f"search/issues?q={urllib.parse.quote(q)}&per_page=1") or {}
    return found.get("total_count", 0) > 0


def body(waiting):
    rows = "\n".join(
        f"- **{a['severity']}** `{a['package']}` in {a['repo']}: {a['summary']} "
        f"({'fixed in ' + a['fixed_in'] if a['fixed_in'] else 'no fixed version yet'}; "
        f"[alert]({a['url']}), open since {a['created_at'][:10]})" for a in waiting)
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
    waiting = [a for a in stale(json.load(open(path)), now or datetime.datetime.now(datetime.timezone.utc))
               if not has_fix_pr(a)]
    print(keep(REPO, LABEL, TITLE, body(waiting) if waiting else None,
               passed="No alert is waiting without a fix PR any more. Closing."))


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ("collect", "report"):
        sys.exit(__doc__)
    (collect if sys.argv[1] == "collect" else report)(sys.argv[2])


if __name__ == "__main__":
    main()

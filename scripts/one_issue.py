"""Keep one open issue per scheduled report (design 6.8, "Every hand-off goes to an assigned
human"), shared by report_drift.py and design_refs.py.

The issue is found by its source label. With problems, it is opened (a filled-in form body, its
type, the source label, assigned to the automation owners in rulesets/teams.json) or updated if the
text changed; an update re-adds the owners if nobody is assigned. With none, any open one is closed
with a comment. Runs with the automation app's token, so the issue-fields automation puts the
issue on the board.
"""

from rules import automation_owners, ensure_label, gh


def new_issue(title, label, text, issue_type="Task"):
    """The REST payload that opens the issue."""
    return {"title": title, "body": text, "type": issue_type, "labels": [label],
            "assignees": automation_owners()}


def update(issue, text):
    """The REST PATCH payload for the open issue, or None if nothing changes."""
    change = {}
    if (issue["body"] or "").replace("\r\n", "\n") != text:
        change["body"] = text
    if not issue.get("assignees"):
        change["assignees"] = automation_owners()
    return change or None


def open_issue(repo, label):
    # The issues endpoint lists PRs too, and Renovate's PRs share the `dependencies` label: skip them.
    found = [i for i in gh(f"repos/{repo}/issues?labels={label}&state=open&per_page=100") or []
             if "pull_request" not in i]
    return found[0] if found else None


def keep(repo, label, title, text, passed="The daily check passes again. Closing."):
    """Open or update the issue with `text`, or close it when `text` is None. Returns what it did,
    as a line to print."""
    issue = open_issue(repo, label)
    if text is None:
        if not issue:
            return "nothing to report"
        gh(f"repos/{repo}/issues/{issue['number']}/comments", "-X", "POST",
           body={"body": passed})
        gh(f"repos/{repo}/issues/{issue['number']}", "-X", "PATCH",
           body={"state": "closed", "state_reason": "completed"})
        return f"closed #{issue['number']}"
    if issue is None:
        ensure_label(repo, label)
        made = gh(f"repos/{repo}/issues", "-X", "POST", body=new_issue(title, label, text))
        return f"opened #{made['number']}"
    change = update(issue, text)
    if change:
        gh(f"repos/{repo}/issues/{issue['number']}", "-X", "PATCH", body=change)
        return f"updated #{issue['number']}"
    return f"#{issue['number']} already lists these problems"

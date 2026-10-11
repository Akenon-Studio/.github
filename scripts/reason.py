"""The board's Reason field (design 6.6): why an open issue or PR waits on a person, so Needs a
human can show everything that does, grouped by why. The automation sets and clears it, never a
person. An item has the first Reason that applies:

1. Needs fields: labelled `needs-fields` (issue_fields.py); its comment says what to fix.
2. Review: board Status In review (a ready PR closes the issue), or a bot's PR that closes no
   issue (dependency updates, release-please's Release PR).
3. Report: an issue a bot in `report_authors` opened for a person (the scorecard, settings drift).
4. New bug: a bug report still carrying the triage label.
5. Waiting on: anything else in Waiting for human.

The option names are the Reason field's in rulesets/board.json; the rules beyond a label or a
Status are its `reasons` block. A bot's open issue, or a bot's PR that closes no issue, that gets
no Reason is a `needs-fields` problem (unless it carries an exempt label, like Peras's plan
sub-issues), so new bot output can't miss Needs a human.
"""

from board import spec
from rules import bot_login

FIELD = "Reason"
NEEDS_FIELDS, REVIEW, REPORT, NEW_BUG, WAITING_ON = (
    "Needs fields", "Review", "Report", "New bug", "Waiting on")
FLAG = "needs-fields"  # issue_fields.LABEL; not imported, since issue_fields imports this


def rules():
    return spec()["reasons"]


def is_bot(author):
    return (author or {}).get("__typename") == "Bot"


def exempt(labels, rule):
    return bool(set(labels) & set(rule["exempt_labels"]))


def issue_reason(issue_type, labels, status, author, rule):
    """The Reason for an open issue, or None. `labels` are names, `author` the GraphQL actor."""
    labels = set(labels)
    if FLAG in labels:
        return NEEDS_FIELDS
    if status == "In review":
        return REVIEW
    if is_bot(author) and bot_login(author.get("login")) in rule["report_authors"]:
        return REPORT
    new_bug = rule["new_bug"]
    if issue_type == new_bug["type"] and new_bug["label"] in labels:
        return NEW_BUG
    if status == "Waiting for human":
        return WAITING_ON
    return None


def issue_problems(issue_type, labels, status, author, rule):
    """A bot's issue that no rule gives a Reason would wait on nobody: a problem to flag."""
    if not is_bot(author) or exempt(labels, rule):
        return []
    if issue_reason(issue_type, set(labels) - {FLAG}, status, author, rule):
        return []
    return [f"Opened by @{author.get('login')}, a bot or app, but no Reason applies to it, so it "
            "would not show in the board's Needs a human view. Add the bot to `report_authors` in "
            "rulesets/board.json in the .github repo if its issues are reports for a person, or to "
            "`exempt_labels` with its label if they wait on no one (design 6.6)."]


def pr_on_board(pr, rule):
    """True for a bot's open, ready PR that goes on the board itself: it closes no issue, so no
    issue carries its Reason (design 6.8). A draft waits on no one yet (6.6)."""
    labels = {l["name"] for l in pr["labels"]["nodes"]}
    return (is_bot(pr["author"]) and not pr.get("isDraft") and not exempt(labels, rule)
            and not pr["closingIssuesReferences"]["totalCount"])


def pr_problems(pr, sources):
    """A bot PR that closes no issue must be from a bot listed as opening such PRs (rulesets/
    bots.json `no_issue`); any other is a problem to flag."""
    source = sources.get(bot_login(pr["author"]["login"])) or {}
    if source.get("no_issue"):
        return []
    return [f"A PR from @{pr['author']['login']}, a bot or app, that closes no issue. List the bot "
            "in rulesets/bots.json in the .github repo with `no_issue` if its PRs never have one "
            "(dependency updates, release PRs), or have it close the issue it works on "
            "(design 6.8)."]


def pr_reason(labels):
    """The Reason for a bot's PR on the board (pr_on_board): Needs fields if flagged, else Review."""
    return NEEDS_FIELDS if FLAG in set(labels) else REVIEW

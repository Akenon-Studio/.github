#!/usr/bin/env python3
"""Ask the automation owners team to review every open PR a bot or app opened (design 6.8): a PR
nobody is asked to review notifies no one. Runs on the issue-sweep schedule, as the org app, so it
needs no secrets in each repo's callers and also covers PRs whose own workflow runs get no secrets
(Dependabot's). A PR is asked once: if the team was ever requested (the timeline shows it), a
later removal or a finished review is left alone.

A CODEOWNERS entry was not used: it cannot tell a bot's PR from a person's, so it would also ask
the team to review people's changes to the same files.

It also adds a listed bot's source label (rulesets/bots.json) to its PR when the PR carries none of
them, so every bot PR says where it came from (design 6.8).

A bot's PR that closes no issue (Renovate's, release-please's Release PR) has no issue to carry it
on the board, so it goes on the board itself: Status In review, Reason Review, so it shows in Needs
a human (design 6.6, 6.8; scripts/reason.py). The board marks it Done when it merges or closes. One
from a bot not listed with `no_issue` in rulesets/bots.json is flagged `needs-fields` with a comment
saying what to fix, as issue_fields.py flags an issue, and gets Reason Needs fields. A PR with an
exempt label (Peras's, which close their intent) is left off, and so is a draft. A bot PR on the
board that stops qualifying (now a draft, or closing an issue) has its Reason cleared and its flag
removed, so it leaves Needs a human.

The review requests run first: they are the hand-off notice, so a board error must not stop them.
An error on one PR's board item is reported and the rest carry on.

Usage: scripts/bot_pr_review.py [--dry-run]
"""

import sys

import reason
from board import find_board_fields, graphql, spec
from issue_fields import LABEL, report, set_field, set_reason
from rules import AUTOMATION_OWNERS, ORG, bot_login, bot_sources, gh, managed_repos

PR_FIELDS = """id number isDraft author { __typename login } repository { nameWithOwner }
  labels(first: 50) { nodes { name } }
  closingIssuesReferences(first: 1) { totalCount }
  projectItems(first: 20) { nodes { id project { id }
    status: fieldValueByName(name: "Status") { ... on ProjectV2ItemFieldSingleSelectValue { name } }
    reason: fieldValueByName(name: "Reason") { ... on ProjectV2ItemFieldSingleSelectValue { name } } } }
  timelineItems(itemTypes: [REVIEW_REQUESTED_EVENT], first: 100) { nodes {
    ... on ReviewRequestedEvent { requestedReviewer { __typename ... on Team { slug } } } } }"""


def open_prs():
    """Every open PR in the org, with what needs_review() reads."""
    nodes, after = [], None
    while True:
        data = graphql(f"""query($q: String!, $after: String) {{
            search(type: ISSUE, query: $q, first: 50, after: $after) {{
              pageInfo {{ hasNextPage endCursor }}
              nodes {{ ... on PullRequest {{ {PR_FIELDS} }} }} }} }}""",
                       q=f"org:{ORG} is:pr is:open", after=after)
        nodes += [n for n in data["search"]["nodes"] if n]
        if not data["search"]["pageInfo"]["hasNextPage"]:
            return nodes
        after = data["search"]["pageInfo"]["endCursor"]


def needs_review(pr, managed, team=AUTOMATION_OWNERS):
    """True for a bot's open PR in a managed repo that the team was never asked to review."""
    owner, name = pr["repository"]["nameWithOwner"].split("/")
    if owner.lower() != ORG or name not in managed:
        return False
    if (pr["author"] or {}).get("__typename") != "Bot":
        return False
    asked = [(e.get("requestedReviewer") or {}).get("slug") for e in pr["timelineItems"]["nodes"]]
    return team not in asked


def missing_source_label(pr, managed, sources):
    """The label to add to a listed bot's open PR that carries none of its source labels, or None."""
    owner, name = pr["repository"]["nameWithOwner"].split("/")
    author = pr["author"] or {}
    if owner.lower() != ORG or name not in managed or author.get("__typename") != "Bot":
        return None
    source = sources.get(bot_login(author.get("login")))
    have = {l["name"] for l in pr["labels"]["nodes"]}
    if not source or have & set(source["labels"]):
        return None
    return source["labels"][0]


def on_board(board_id, pr):
    """(item id, Status, Reason) of the PR's board item, or None if it isn't on the board."""
    for item in pr["projectItems"]["nodes"]:
        if item["project"]["id"] == board_id:
            return (item["id"], (item.get("status") or {}).get("name"),
                    (item.get("reason") or {}).get("name"))
    return None


def board_changes(pr, board_id, problems):
    """What a bot's issue-less PR needs on the board: (its item as on_board() reads it, or None to
    add it; Status to set, or None to keep its own; the Reason it should have). `problems` are
    reason.pr_problems()'s, which flag it."""
    found = on_board(board_id, pr)
    labels = {l["name"] for l in pr["labels"]["nodes"]} - {LABEL}
    want = reason.pr_reason(labels | ({LABEL} if problems else set()))
    return found, (None if found and found[1] else "In review"), want


def put_on_board(pr, board, sources, dry_run):
    """Put a bot's issue-less PR on the board in In review with its Reason, flagging it if no
    listed bot opened it. Returns a line to print, or None if nothing changed."""
    repo, n = pr["repository"]["nameWithOwner"], pr["number"]
    problems = reason.pr_problems(pr, sources)
    found, status, want = board_changes(pr, board["id"], problems)
    flagged = LABEL in {l["name"] for l in pr["labels"]["nodes"]}
    if found and status is None and found[2] == want and flagged == bool(problems):
        return None
    if dry_run:
        return f"{repo}#{n}: would put on the board ({status or 'Status kept'}, Reason {want})"
    report(repo, n, pr, problems, "PR")
    item = found[0] if found else graphql("""mutation($p: ID!, $c: ID!) { addProjectV2ItemById(
        input: {projectId: $p, contentId: $c}) { item { id } } }""",
                                          p=board["id"], c=pr["id"])["addProjectV2ItemById"]["item"]["id"]
    if status:
        set_field(board, item, "Status", status)
    set_reason(board, item, found[2] if found else None, want)
    return f"{repo}#{n}: on the board, {status or 'Status kept'}, Reason {want}"


def take_off(pr, board, dry_run):
    """Clear the Reason of a bot PR on the board that no longer qualifies (pr_on_board), and remove
    its flag. Returns a line to print, or None if there was nothing to clear."""
    found = on_board(board["id"], pr)
    flagged = LABEL in {l["name"] for l in pr["labels"]["nodes"]}
    if not (found and found[2]) and not flagged:
        return None
    repo, n = pr["repository"]["nameWithOwner"], pr["number"]
    if dry_run:
        return f"{repo}#{n}: would clear its Reason"
    if flagged:
        report(repo, n, pr, [], "PR")
    if found:
        set_reason(board, found[0], found[2], None)
    return f"{repo}#{n}: no longer waits on a person; Reason cleared"


def board_pass(prs, managed, sources, dry_run):
    """Put each bot's issue-less PR on the board, and take off those that no longer qualify.
    Returns how many PRs failed."""
    rule, board, failed = reason.rules(), None, 0
    for pr in prs:
        owner, name = pr["repository"]["nameWithOwner"].split("/")
        if owner.lower() != ORG or name not in managed or not reason.is_bot(pr["author"]):
            continue
        qualifies = reason.pr_on_board(pr, rule)
        if not qualifies and not pr["projectItems"]["nodes"]:
            continue
        try:
            board = board or find_board_fields(spec()["title"])
            done = (put_on_board(pr, board, sources, dry_run) if qualifies
                    else take_off(pr, board, dry_run))
        except (SystemExit, Exception) as e:  # gh() and graphql() exit on an API error
            failed += 1
            print(f"::error::{pr['repository']['nameWithOwner']}#{pr['number']}: board update "
                  f"failed: {e}")
            continue
        if done:
            print(done)
    return failed


def main():
    dry_run = "--dry-run" in sys.argv[1:]
    managed = set(managed_repos())
    sources = bot_sources()
    prs = open_prs()
    for pr in prs:
        label = missing_source_label(pr, managed, sources)
        if label:
            repo, n = pr["repository"]["nameWithOwner"], pr["number"]
            if not dry_run:
                gh(f"repos/{repo}/issues/{n}/labels", "-X", "POST", body={"labels": [label]})
            print(f"{repo}#{n}: {'would add' if dry_run else 'added'} source label {label}")
    todo = [pr for pr in prs if needs_review(pr, managed)]
    for pr in todo:
        repo, n = pr["repository"]["nameWithOwner"], pr["number"]
        if not dry_run:
            gh(f"repos/{repo}/pulls/{n}/requested_reviewers", "-X", "POST",
               body={"team_reviewers": [AUTOMATION_OWNERS]})
        print(f"{repo}#{n} ({pr['author']['login']}): {'would ask' if dry_run else 'asked'} "
              f"{AUTOMATION_OWNERS} to review")
    if not todo:
        print("Every bot PR has had a review requested.")
    if board_pass(prs, managed, sources, dry_run):
        sys.exit(1)


if __name__ == "__main__":
    main()

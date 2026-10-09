#!/usr/bin/env python3
"""Re-check issues on a schedule, for changes that send no event every repo's caller listens to
(design 6.8): a board Status set to Blocked or Waiting for human, an assignee added or removed, a parent or "blocked by" link added or removed, a
parent closed while its sub-issues are open, a parent's last open sub-issue closed (which sends the
parent no event). Runs the issue_fields.py checks on every open issue in
the managed repos and syncs only those whose `needs-fields` label or comment is out of date, then
reopens any issue closed in the last few hours that must stay open: a parent with open sub-issues,
or a Process failure whose prevention is not decided yet.

It also checks every open PR in the managed repos for a conflict with its base branch, which
GitHub tells nobody about (design 6.8, "Every hand-off goes to an assigned human"). A conflicting
PR gets one comment, found again by PR_MARKER, that mentions its assignees (for a bot's PR with
none, the automation owners), lists the files changed on both sides and says how to fix it. The
comment is edited if the files change, and edited to say "Resolved" once the PR merges cleanly. A
PR that conflicts again after that gets a new comment, because an edit notifies nobody.

Usage: scripts/issue_sweep.py [--dry-run]
--dry-run changes nothing and prints what it would do.
In Actions it runs from the issue-sweep workflow with the org GitHub App token in GH_TOKEN.
"""

import datetime
import json
import sys

from board import find_board_fields, graphql, spec
from issue_fields import (ISSUE_FIELDS, LABEL, MARKER, all_problems, closed_too_early, load_forms,
                          problems_text, status_on, sync)
from rules import ORG, automation_owners, gh, managed_repos, try_gh

CLOSED_WINDOW = datetime.timedelta(hours=6)  # covers late or skipped scheduled runs
ISSUE_SEARCH = f"... on Issue {{ {ISSUE_FIELDS} comments(first: 100) {{ nodes {{ body }} }} }}"

PR_MARKER = "<!-- pr-conflicts -->"
RESOLVED = "**Resolved:**"
# mergeable is CONFLICTING when mergeStateStatus is DIRTY. GitHub computes it lazily: UNKNOWN means
# not computed yet (this query starts it), so the PR is skipped and caught on the next run.
PR_SEARCH = """... on PullRequest { number mergeable baseRefName headRefOid
  author { __typename login } repository { nameWithOwner }
  assignees(first: 20) { nodes { login } }
  comments(last: 100) { nodes { databaseId body } } }"""


def search(query, fields=ISSUE_SEARCH):
    """Every issue (or, with PR_SEARCH, PR) matching a search, with what the checks read and its
    comments."""
    nodes, after = [], None
    while True:
        data = graphql(f"""query($q: String!, $after: String) {{
            search(type: ISSUE, query: $q, first: 50, after: $after) {{
              pageInfo {{ hasNextPage endCursor }}
              nodes {{ {fields} }} }} }}""", q=query, after=after)
        page = data["search"]
        nodes += [n for n in page["nodes"] if n]
        if not page["pageInfo"]["hasNextPage"]:
            return nodes
        after = page["pageInfo"]["endCursor"]


def out_of_date(issue, forms, board_id):
    """True if the issue's label or comment doesn't match what the checks find now."""
    problems = all_problems(issue, forms, status_on(board_id, issue))
    labelled = LABEL in {l["name"] for l in issue["labels"]["nodes"]}
    ours = [c["body"] for c in issue["comments"]["nodes"] if c["body"].startswith(MARKER)]
    if bool(problems) != labelled:
        return True
    return bool(problems) and (not ours or ours[0] != problems_text(problems))


def in_managed_repo(issue, managed):
    owner, name = issue["repository"]["nameWithOwner"].split("/")
    return owner.lower() == ORG and name in managed


def who_to_tell(pr):
    """The logins a conflict notice mentions: the PR's assignees; with none, the automation owners
    for a bot's PR and the author for a person's."""
    assignees = [a["login"] for a in pr["assignees"]["nodes"]]
    if assignees:
        return assignees
    author = pr["author"] or {}
    return automation_owners() if author.get("__typename") == "Bot" else [author["login"]]


def both_sides(pr_files, base_files):
    """Files changed on the PR's branch and on its base since they split: where the conflicts are."""
    return sorted(set(pr_files) & set(base_files))


def changed_since_split(repo, since, to):
    """Files changed on `to` since it split from `since` (a three-dot compare, which lists up to
    300 files), or None if the call fails."""
    found = try_gh(f"repos/{repo}/compare/{since}...{to}")
    return [f["filename"] for f in found["files"]] if found and "files" in found else None


def conflict_files(pr):
    """Files changed on both the PR's branch and its base branch since they split, or None if they
    cannot be read. Two REST calls, made only for a conflicting PR. The base is compared by name:
    baseRefOid is where the base was when the PR last changed, not where it is now."""
    repo, head, base = pr["repository"]["nameWithOwner"], pr["headRefOid"], pr["baseRefName"]
    on_base = changed_since_split(repo, head, base)
    on_branch = changed_since_split(repo, base, head) if on_base is not None else None
    return both_sides(on_branch, on_base) if on_branch is not None else None


def conflict_text(pr, files):
    """The notice for a conflicting PR; `files` may be empty when they could not be worked out."""
    base = pr["baseRefName"]
    where = (f"These files changed both on this branch and on `{base}` since it branched off, so "
             f"the conflicts are in them:\n\n" + "\n".join(f"- `{f}`" for f in files) + "\n\n"
             if files else "")
    return (f"{PR_MARKER}\n{' '.join('@' + p for p in who_to_tell(pr))} This PR conflicts with "
            f"`{base}`, so it cannot merge.\n\n{where}"
            f"To fix it, merge `{base}` into this branch and resolve the conflicts:\n\n"
            f"```sh\ngit fetch origin\ngit merge origin/{base}\n"
            f"# fix each file git lists, then: git add <files> && git commit\ngit push\n```\n\n"
            f"Please don't rebase and force-push: that rewrites commits reviewers have already seen. "
            f"The issue sweep checks again every 15 minutes and edits this comment when the conflict "
            f"is gone.")


def resolved_text(pr, earlier):
    """The notice once the PR no longer conflicts, keeping the earlier notice below it."""
    old = earlier.removeprefix(PR_MARKER).strip()
    return (f"{PR_MARKER}\n{RESOLVED} this PR no longer conflicts with `{pr['baseRefName']}`.\n\n"
            f"<details><summary>The earlier notice</summary>\n\n{old}\n\n</details>")


def our_pr_comment(pr):
    """The PR's latest conflict notice, or None."""
    ours = [c for c in pr["comments"]["nodes"] if c["body"].startswith(PR_MARKER)]
    return ours[-1] if ours else None


def pr_action(pr, files=None):
    """What the conflict notice needs: ("post", text), ("edit", comment id, text) or None.
    `files` is conflict_files(pr), read only for a conflicting PR."""
    comment = our_pr_comment(pr)
    live = comment is not None and not comment["body"].startswith(f"{PR_MARKER}\n{RESOLVED}")
    if pr["mergeable"] == "CONFLICTING":
        text = conflict_text(pr, files or [])
        if not live:
            return ("post", text)  # first conflict, or a new one after a resolved notice
        return ("edit", comment["databaseId"], text) if comment["body"] != text else None
    if pr["mergeable"] == "MERGEABLE" and live:
        return ("edit", comment["databaseId"], resolved_text(pr, comment["body"]))
    return None  # UNKNOWN: not computed yet


def check_prs(managed, dry_run):
    """Post or update the conflict notice on every open PR in the managed repos that needs it."""
    results = []
    for pr in search(f"org:{ORG} is:pr is:open", PR_SEARCH):
        if not in_managed_repo(pr, managed):
            continue
        action = pr_action(pr, conflict_files(pr) if pr["mergeable"] == "CONFLICTING" else None)
        if action is None:
            continue
        repo, n = pr["repository"]["nameWithOwner"], pr["number"]
        if not dry_run:
            if action[0] == "post":
                gh(f"repos/{repo}/issues/{n}/comments", "-X", "POST", body={"body": action[1]})
            else:
                gh(f"repos/{repo}/issues/comments/{action[1]}", "-X", "PATCH",
                   body={"body": action[2]})
        results.append({"pr": f"{repo}#{n}", "mergeable": pr["mergeable"],
                        "comment": action[0], "text": action[-1]})
    return results


def issue_sync(issue, forms, board, dry_run):
    """sync() the issue, or with --dry-run only name it."""
    repo, n = issue["repository"]["nameWithOwner"], issue["number"]
    return {"issue": f"{repo}#{n}", "would sync": True} if dry_run else sync(repo, n, forms, board)


def main():
    dry_run = "--dry-run" in sys.argv[1:]
    forms, managed = load_forms(), set(managed_repos())
    board = find_board_fields(spec()["title"])
    results = []
    for issue in search(f"org:{ORG} is:issue is:open"):
        if in_managed_repo(issue, managed) and out_of_date(issue, forms, board["id"]):
            results.append(issue_sync(issue, forms, board, dry_run))
    since = (datetime.datetime.now(datetime.timezone.utc) - CLOSED_WINDOW).strftime("%Y-%m-%dT%H:%M:%SZ")
    for issue in search(f"org:{ORG} is:issue is:closed closed:>={since}"):
        if in_managed_repo(issue, managed) and closed_too_early(issue):
            results.append(issue_sync(issue, forms, board, dry_run))
    results += check_prs(managed, dry_run)
    print(json.dumps(results, indent=2) if results else "Every issue and PR is up to date.")


if __name__ == "__main__":
    main()

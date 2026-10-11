#!/usr/bin/env python3
"""Put an issue on the org board with its fields filled from the form answers, and flag anything
missing (design 6.6). The issue forms in .github/ISSUE_TEMPLATE/ are the only source of what is
required: an issue's type picks its form, every required question must have an answer, dropdown
answers must be one of the options, and a question labelled "(required when Kind is Bug fix)" is
required when that answer is given.

Dropdowns whose label is also a board field (Discipline, Phase, Priority, Audit, Severity) are
copied to the board. New items start in Todo; Decisions, bug reports, critical or high Audit
findings and the reports the automation opens in Waiting for human. The board's Reason field (why
the item waits on a person, scripts/reason.py) is set or cleared on every run. An issue with problems gets the `needs-fields` label and one comment listing
them; once fixed, the label goes and the comment says so. The issue body is the source of truth: edit the answer there and the
board follows.

Links between issues are checked too (design 6.8): every issue has a parent unless it is top level
(an Epic, which only a person creates, design 6.6; or an Intent), a Bug report not yet triaged,
or an issue a bot opened (the org's GitHub App, e.g. the settings-drift report), and an epic has none; an issue whose
board Status is Blocked has a "blocked by" link; an open issue whose sub-issues are all closed is
flagged, since a parent holds no work of its own (close it, or add a sub-issue for the work left);
and a closed issue with open sub-issues is reopened with a comment naming them.

Who is assigned is checked too (design 6.8, "Every hand-off goes to an assigned human"): an issue a
person opened must have its author assigned, and an issue whose board Status is Waiting for human
must have a person (not a bot or app) assigned. "The author is an assignee at creation" is checked
in the simplest form that holds up: the author counts as assigned if they are an assignee now or
were ever assigned (an `assigned` event in the issue's timeline). So the problem is fixed by
assigning the author, who may then hand the issue to someone else. Issues a bot or app opens are
exempt from the author rule.

A critical or high Audit finding needs a person to confirm it before work starts (design 6.1): it
starts in Waiting for human, assigned to its author, and is flagged until a person (not a bot or
app) applies the `confirmed` label. Who applied it is read from the issue's timeline, so a label a
bot added doesn't count. Labelling sends no event the callers listen to, so issue_sweep.py picks
the confirmation up on its next run.

A Process failure closed while its answer to "What now
prevents this, by code?" is "Not decided yet" is reopened too. Board changes, links and closing send no event every repo's
caller listens to (closing a sub-issue sends none to its parent), so scripts/issue_sweep.py re-checks on a schedule.

Usage: scripts/issue_fields.py <owner/repo> <issue number>
In Actions it runs from the issue-fields workflow with an org GitHub App token in GH_TOKEN.
"""

import json
import pathlib
import re
import sys
import time

import yaml

import reason
from board import find_board_fields, graphql, spec
from rules import ROOT, bot_login, ensure_label, gh, source_labels

FORMS_DIR = ROOT / ".github" / "ISSUE_TEMPLATE"
LABEL = "needs-fields"
MARKER = "<!-- issue-fields -->"
REOPEN_MARKER = "<!-- issue-fields: reopened -->"
RECLOSE_MARKER = "<!-- issue-fields: closed again -->"
AUTOMATION_APP = "akenon-studio-automation"  # the org app this workflow signs in as
EMPTY = {"", "_No response_"}
REQUIRED_WHEN = re.compile(r"\(required when (.+?) is (.+?)\)")
# A person decides a Decision and triages a bug report first (design 6.6); everything else starts in Todo
START_STATUS = {"Decision": "Waiting for human", "Bug": "Waiting for human"}
CONFIRMED = "confirmed"
NEEDS_CONFIRMING = {"Critical", "High"}  # Audit finding severities a person confirms (design 6.1)


def load_forms(directory=FORMS_DIR):
    """{issue type: [question]}, each question {label, kind, required, options, multiple,
    required_when}."""
    forms = {}
    for path in sorted(pathlib.Path(directory).glob("*.yml")):
        form = yaml.safe_load(path.read_text())
        if "type" not in form:
            continue  # config.yml
        questions = []
        for item in form["body"]:
            if item["type"] == "markdown":
                continue
            attrs = item["attributes"]
            cond = REQUIRED_WHEN.search(attrs["label"])
            questions.append({
                "label": attrs["label"],
                "kind": item["type"],
                "required": bool(item.get("validations", {}).get("required")),
                "options": [str(o) for o in attrs.get("options", [])],
                "multiple": bool(attrs.get("multiple")),
                "required_when": cond.groups() if cond else None,
            })
        forms[form["type"]] = questions
    return forms


def parse_body(text):
    """{heading: answer} from a body GitHub rendered from a form. Empty answers are ''."""
    answers = {}
    for block in re.split(r"^### ", text or "", flags=re.M)[1:]:
        label, _, value = block.partition("\n")
        value = value.strip()
        fence = re.fullmatch(r"```\w*\n(.*?)\n?```", value, flags=re.S)
        if fence:
            value = fence.group(1).strip()
        answers[label.strip()] = "" if value in EMPTY else value
    return answers


def chosen(question, answer):
    return [a.strip() for a in answer.split(",")] if question["multiple"] else [answer]


def check(issue_type, text, forms):
    """Problems with an issue, as sentences for the comment. Empty means complete."""
    if not issue_type:
        return ["The issue has no type. Set one of: " + ", ".join(sorted(forms)) + "."]
    if issue_type not in forms:
        return [f"The issue type **{issue_type}** has no form. Use one of: "
                + ", ".join(sorted(forms)) + "."]
    answers = parse_body(text)
    problems = []
    for q in forms[issue_type]:
        answer = answers.get(q["label"], "")
        if not answer:
            if q["required"]:
                problems.append(f"**{q['label']}** is required but empty.")
            elif q["required_when"]:
                field, value = q["required_when"]
                if answers.get(field) == value:
                    problems.append(f"**{q['label']}** is required when {field} is {value}, "
                                    "but empty.")
            continue
        if q["kind"] == "dropdown":
            bad = [a for a in chosen(q, answer) if a not in q["options"]]
            for a in bad:
                problems.append(f"**{q['label']}** has `{a}`, which is not one of the options: "
                                + ", ".join(q["options"]) + ".")
    return problems


def needs_confirming(issue_type, text):
    """True for an Audit finding whose Severity is one a person must confirm (design 6.1)."""
    return issue_type == "Audit finding" and parse_body(text).get("Severity") in NEEDS_CONFIRMING


def start_status(issue_type, text, author=None, rule=None):
    """The board Status a new item starts in: Waiting for human for a Decision, a bug report, a
    critical or high Audit finding (a person confirms it first) and a report the automation opened
    (`author` is the GraphQL actor), Todo for everything else."""
    if needs_confirming(issue_type, text):
        return "Waiting for human"
    if reason.is_bot(author) and bot_login(author.get("login")) in (rule or reason.rules())[
            "report_authors"]:
        return "Waiting for human"
    return START_STATUS.get(issue_type, "Todo")


def confirmed_by_person(labels, label_events):
    """True if the `confirmed` label is on the issue and the last time it was applied, a person
    (not a bot or app) applied it. `labels` are the label names now; `label_events` the timeline's
    LabeledEvent nodes, oldest first."""
    if CONFIRMED not in labels:
        return False
    applied = [e for e in label_events if (e.get("label") or {}).get("name") == CONFIRMED]
    return bool(applied) and ((applied[-1].get("actor") or {}).get("__typename") == "User")


def confirmation_problems(issue_type, text, confirmed):
    """Problems with an Audit finding's confirmation (design 6.1). `confirmed` is
    confirmed_by_person()'s answer."""
    if not needs_confirming(issue_type, text) or confirmed:
        return []
    severity = parse_body(text)["Severity"]
    return [f"A **{severity}** audit finding needs a person to confirm it before work starts: "
            f"check the evidence and apply the `{CONFIRMED}` label (design 6.1). A label a bot or "
            "app applied doesn't count. If the finding is false, close it and re-check that "
            "agent's batch."]


# The top level (design 6.6): epics, which only a person creates, and intents.
NO_PARENT_TYPES = {"Epic", "Intent", "Bug"}  # Bug: a report needs no parent until it is triaged


def needs_parent(issue_type, by_bot=False):
    """False for issues design 6.8 lets stand alone."""
    return not (issue_type in NO_PARENT_TYPES or by_bot)


def link_problems(issue_type, has_parent, status, blocked_by, by_bot=False):
    """Problems with an issue's links (design 6.8, checks 3 and 4). `blocked_by` is a count."""
    problems = []
    if not has_parent and needs_parent(issue_type, by_bot):
        problems.append("The issue has no parent. Add it as a sub-issue of the epic, step or part "
                        "it belongs to (deferred findings and later-phase work go under handbook#83, "
                        "Deferred work). Only epics, intents and untriaged bugs stand alone.")
    if has_parent and issue_type == "Epic":
        problems.append("An epic is top level (design 6.6): remove its parent, or make it a Task "
                        "if it is a part of another epic.")
    if status == "Blocked" and not blocked_by:
        problems.append("Status is **Blocked** but nothing is linked as blocking it. Add a "
                        "\"blocked by\" link (Relationships, in the sidebar) to the issue it waits on.")
    return problems


FINISHED_PARENT = ("All its sub-issues are closed: close it, or add a sub-issue for the work left "
                   "(design 6.8).")


def finished_parent_problems(state, sub_issues_total, sub_issues_closed):
    """An open issue whose sub-issues are all closed (design 6.8, check 2): a parent holds no work
    of its own, so either it is done or what is left needs its own sub-issue. The counts are
    GitHub's subIssuesSummary total and completed (completed counts every closed sub-issue,
    whatever the close reason)."""
    if state == "OPEN" and sub_issues_total and sub_issues_closed >= sub_issues_total:
        return [FINISHED_PARENT]
    return []


WAITING = "Waiting for human"


def assignee_problems(author, by_bot, assignees, ever_assigned, status):
    """Problems with who is assigned (design 6.8). `assignees` are the people assigned now and
    `ever_assigned` everyone the timeline shows was assigned, both as logins; bots never appear in
    `assignees`."""
    problems = []
    known = {a.lower() for a in [*assignees, *ever_assigned]}
    if author and not by_bot and author.lower() not in known:
        problems.append(f"The author, @{author}, is not assigned. Assign @{author} (Assignees, in "
                        "the sidebar); you can hand the issue to someone else after. Whoever opens "
                        "an issue is assigned to it, so a hand-off reaches a person.")
    if status == WAITING and not assignees:
        problems.append(f"Status is **{WAITING}** but no person is assigned. Assign the person it "
                        "waits on; a bot or app doesn't count, since status alone notifies no one.")
    return problems


def people_assigned(issue):
    """(people assigned now, everyone ever assigned) as logins, from the GraphQL fields."""
    now = [a["login"] for a in issue["assignedActors"]["nodes"] if a.get("__typename") == "User"]
    ever = [(e.get("assignee") or {}).get("login") for e in issue["timelineItems"]["nodes"]]
    return now, [e for e in ever if e]


def open_sub_issues(sub_issues):
    """'owner/repo#n' for each open sub-issue, from the GraphQL subIssues nodes."""
    return [f"{s['repository']['nameWithOwner']}#{s['number']}" for s in sub_issues
            if s["state"] == "OPEN"]


PREVENTION = "What now prevents this, by code?"
UNDECIDED = "Not decided yet"


def reopen_reasons(issue_type, text, sub_issues):
    """Why a closed issue must be open again (design 6.8). Empty means it may stay closed."""
    reasons = []
    open_subs = open_sub_issues(sub_issues)
    if open_subs:
        reasons.append("A parent can't close while its sub-issues are open. Close or move these "
                       "first: " + ", ".join(open_subs) + ".")
    if issue_type == "Process failure" and parse_body(text).get(PREVENTION) == UNDECIDED:
        reasons.append(f"A process failure stays open while **{PREVENTION}** is "
                       f"\"{UNDECIDED}\". Add the check, or say why it can't be code, then close it.")
    return reasons


def reopen_text(reasons):
    return f"{REOPEN_MARKER}\nReopened (design 6.8):\n\n" + "\n".join(f"- {r}" for r in reasons)


def board_values(issue_type, text, forms, field_names):
    """{board field: option} for valid dropdown answers whose label is a board field."""
    answers = parse_body(text)
    values = {}
    for q in forms.get(issue_type, []):
        answer = answers.get(q["label"], "")
        if q["kind"] == "dropdown" and not q["multiple"] and q["label"] in field_names \
                and answer in q["options"]:
            values[q["label"]] = answer
    return values


# --- The Epic board field (design 6.6) ---------------------------------------------------------

EPIC_FIELD = "Epic"
EPIC_DEPTH = 6  # parents followed up to an epic: epic > part > work leaves plenty of room


def ancestors(depth=EPIC_DEPTH):
    """The GraphQL for an issue's parent chain, `depth` levels up."""
    node = "number title issueType { name } repository { nameWithOwner }"
    for _ in range(depth - 1):
        node = f"number title issueType {{ name }} repository {{ nameWithOwner }} parent {{ {node} }}"
    return node


def epic_of(issue):
    """(ref, title) of the topmost Epic at or above the issue, or None if there is none."""
    found, node = None, issue
    while node:
        if (node.get("issueType") or {}).get("name") == "Epic":
            found = (f"{node['repository']['nameWithOwner'].split('/')[1]}#{node['number']}",
                     node["title"])
        node = node.get("parent")
    return found


def epic_options(live_options, ref, title):
    """The Epic field's options with this epic's option present and named `title`, found by its
    description (the epic's repo#n, so a renamed epic keeps its option), or None if nothing
    changes. Existing options keep their IDs, so values already set survive the update."""
    keep = [{k: o[k] for k in ("id", "name", "color", "description")} for o in live_options]
    mine = next((o for o in keep if o["description"] == ref), None)
    if mine and mine["name"] == title:
        return None
    if mine:
        mine["name"] = title
    else:
        keep.append({"name": title, "color": "GRAY", "description": ref})
    return keep


def epic_stale(board_id, issue):
    """True if the board's Epic value for the issue differs from the epic its parents lead to."""
    want = epic_of(issue)
    return (want[1] if want else None) != epic_on(board_id, issue)


# --- GitHub side -------------------------------------------------------------------------------

# What the checks read about an issue; issue_sweep.py reads the same for many issues at once.
ISSUE_FIELDS = """id number title state body author { __typename login } issueType { name } labels(first: 50) { nodes { name } }
  repository { nameWithOwner }
  parent { ANCESTORS }
  subIssues(first: 50) { nodes { number state repository { nameWithOwner } } }
  subIssuesSummary { total completed }
  blockedBy(first: 1) { totalCount }
  assignedActors(first: 20) { nodes { __typename ... on User { login } ... on Bot { login } } }
  timelineItems(itemTypes: [ASSIGNED_EVENT], first: 100) { nodes {
    ... on AssignedEvent { assignee { __typename ... on User { login } } } } }
  reopenEvents: timelineItems(itemTypes: [REOPENED_EVENT], last: 1) { nodes {
    ... on ReopenedEvent { actor { __typename login } } } }
  labelEvents: timelineItems(itemTypes: [LABELED_EVENT], last: 100) { nodes {
    ... on LabeledEvent { label { name } actor { __typename login } } } }
  projectItems(first: 20) { nodes { id project { id }
    fieldValueByName(name: "Status") { ... on ProjectV2ItemFieldSingleSelectValue { name } }
    epic: fieldValueByName(name: "Epic") { ... on ProjectV2ItemFieldSingleSelectValue { name } }
    reason: fieldValueByName(name: "Reason") { ... on ProjectV2ItemFieldSingleSelectValue { name } } } }"""
ISSUE_FIELDS = ISSUE_FIELDS.replace("ANCESTORS", ancestors())


def load_issue(repo, number):
    owner, name = repo.split("/")
    data = graphql(f"""query($o: String!, $n: String!, $i: Int!) {{ repository(owner: $o, name: $n) {{
        issue(number: $i) {{ {ISSUE_FIELDS} }} }} }}""", o=owner, n=name, i=int(number))
    return data["repository"]["issue"]


def status_on(board_id, issue):
    """The issue's Status on the board, or None if it isn't there or has none."""
    for item in issue["projectItems"]["nodes"]:
        if item["project"]["id"] == board_id:
            return (item["fieldValueByName"] or {}).get("name")
    return None


def source_problems(author, by_bot, labels, sources):
    """An issue a bot or app opened must carry its source label (design 6.8)."""
    if not by_bot or labels & sources:
        return []
    return [f"Opened by @{author}, a bot or app, with no source label. Add the label for where it "
            f"came from ({', '.join(f'`{l}`' for l in sorted(sources))}), or list the bot in "
            "rulesets/bots.json in the .github repo (design 6.8)."]


def all_problems(issue, forms, status, sources=None, rule=None):
    issue_type = (issue["issueType"] or {}).get("name")
    author = issue["author"] or {}
    by_bot = author.get("__typename") == "Bot"
    confirmed = confirmed_by_person({l["name"] for l in issue["labels"]["nodes"]},
                                    issue["labelEvents"]["nodes"])
    subs = issue["subIssuesSummary"]
    return check(issue_type, issue["body"], forms) + link_problems(
        issue_type, issue["parent"] is not None, status, issue["blockedBy"]["totalCount"],
        by_bot) + finished_parent_problems(
        issue["state"], subs["total"], subs["completed"]) + assignee_problems(
        author.get("login"), by_bot, *people_assigned(issue), status) + confirmation_problems(
        issue_type, issue["body"], confirmed) + source_problems(
        author.get("login"), by_bot, {l["name"] for l in issue["labels"]["nodes"]},
        source_labels() if sources is None else sources) + reason.issue_problems(
        issue_type, {l["name"] for l in issue["labels"]["nodes"]}, status, author,
        rule or reason.rules())


def problems_text(problems, what="issue"):
    """The text of the issue's (or, with what="PR", a bot PR's) one comment for these problems."""
    if not problems:
        return (f"{MARKER}\nAll required answers are filled in. Thanks." if what == "issue"
                else f"{MARKER}\nNothing to fix now: this PR doesn't need to be on the board, or "
                     "has what it needs.")
    return (f"{MARKER}\nThis {what} is missing something the board needs, so it is labelled "
            f"`{LABEL}`" + (" and kept out of planning views" if what == "issue" else "")
            + ". To fix:\n\n" + "\n".join(f"- {p}" for p in problems))


def closed_too_early(issue):
    """reopen_reasons() for a closed issue as GraphQL returns it; empty for an open one."""
    if issue["state"] != "CLOSED":
        return []
    return reopen_reasons((issue["issueType"] or {}).get("name"), issue["body"],
                          issue["subIssues"]["nodes"])


def reopen_if_early(repo, number, issue):
    """Reopen a closed issue that must stay open. True if it did."""
    reasons = closed_too_early(issue)
    if not reasons:
        return False
    gh(f"repos/{repo}/issues/{number}", "-X", "PATCH", body={"state": "open"})
    gh(f"repos/{repo}/issues/{number}/comments", "-X", "POST", body={"body": reopen_text(reasons)})
    issue["state"] = "OPEN"
    return True


def reclose_due(issue):
    """True for an open parent the automation reopened (its last reopen was by AUTOMATION_APP, as
    reopen_if_early does when a PR closes it too early) whose sub-issues are now all closed: its
    PR already finished its own work, so it closes again (design 6.8, check 2). A parent a person
    or another bot reopened stays open."""
    subs = issue["subIssuesSummary"]
    last = (issue.get("reopenEvents") or {}).get("nodes") or []
    return (issue["state"] == "OPEN" and subs["total"] > 0 and subs["completed"] >= subs["total"]
            and bool(last) and is_automation(((last[-1] or {}).get("actor")) or {})
            # and closing now passes every reopen check (a process failure's prevention, too),
            # so it isn't reopened again straight away
            and not reopen_reasons((issue.get("issueType") or {}).get("name"), issue.get("body") or "",
                                   (issue.get("subIssues") or {}).get("nodes") or []))


def is_automation(actor):
    """The automation app itself (a reopen by a deleted account has no actor and is not)."""
    return actor.get("__typename") == "Bot" and bot_login(actor.get("login")) == AUTOMATION_APP


RECLOSE_TEXT = (f"{RECLOSE_MARKER}\nClosed again (design 6.8): this was reopened because it closed "
                "while sub-issues were open, and every sub-issue is now closed.")


def reclose_if_done(repo, number, issue):
    """Close a parent reclose_due() picks, as completed, with a comment. True if it did."""
    if not reclose_due(issue):
        return False
    # Close, then comment: a closed parent is never due again, so a retry can't repeat anything,
    # and each reopen-and-close cycle gets its own comment.
    gh(f"repos/{repo}/issues/{number}", "-X", "PATCH",
       body={"state": "closed", "state_reason": "completed"})
    gh(f"repos/{repo}/issues/{number}/comments", "-X", "POST", body={"body": RECLOSE_TEXT})
    issue["state"] = "CLOSED"
    return True


# --- The Reason board field (design 6.6, scripts/reason.py) ------------------------------------

def reason_on(board_id, issue):
    """The issue's Reason on the board, or None."""
    for item in issue["projectItems"]["nodes"]:
        if item["project"]["id"] == board_id:
            return (item.get("reason") or {}).get("name")
    return None


def want_reason(issue, flagged, status, rule=None):
    """The Reason the issue should have once report() has run, which labels it `needs-fields`
    exactly when `flagged` (it has problems)."""
    if issue["state"] != "OPEN":
        return None
    labels = {l["name"] for l in issue["labels"]["nodes"]} - {LABEL}
    if flagged:
        labels.add(LABEL)
    return reason.issue_reason((issue["issueType"] or {}).get("name"), labels, status,
                               issue["author"] or {}, rule or reason.rules())


def set_reason(board, item_id, current, want):
    """Set the item's Reason to `want`, or clear it for None. Nothing until the board has the
    field, or if it already matches."""
    field = next((f for f in board["fields"]["nodes"] if f.get("name") == reason.FIELD), None)
    if field is None or current == want:
        return
    if want is None:
        graphql("""mutation($p: ID!, $i: ID!, $f: ID!) { clearProjectV2ItemFieldValue(
            input: {projectId: $p, itemId: $i, fieldId: $f}) { clientMutationId } }""",
                p=board["id"], i=item_id, f=field["id"])
    else:
        set_field(board, item_id, reason.FIELD, want)


def sync_reason(board, item_id, issue, status):
    """Bring the issue's Reason in step after its Status changed (pr_status.py, comment_status.py):
    its labels are as loaded, since those scripts don't change them."""
    labels = {l["name"] for l in issue["labels"]["nodes"]}
    set_reason(board, item_id, reason_on(board["id"], issue),
               want_reason(issue, LABEL in labels, status))


def epic_on(board_id, issue):
    """The issue's Epic value on the board, or None."""
    for item in issue["projectItems"]["nodes"]:
        if item["project"]["id"] == board_id:
            return (item.get("epic") or {}).get("name")
    return None


def sync_epic(board, item_id, issue):
    """Set the board's Epic field to the epic the issue's parents lead to, adding or renaming that
    epic's option first. Before changing the options they are re-read: a stale list would drop
    an option added since, and with it every value set to it."""
    field = next((f for f in board["fields"]["nodes"] if f.get("name") == EPIC_FIELD), None)
    if field is None or not epic_stale(board["id"], issue):
        return
    want = epic_of(issue)
    if want is None:
        graphql("""mutation($p: ID!, $i: ID!, $f: ID!) { clearProjectV2ItemFieldValue(
            input: {projectId: $p, itemId: $i, fieldId: $f}) { clientMutationId } }""",
                p=board["id"], i=item_id, f=field["id"])
        return
    if epic_options(field["options"], *want) is not None:
        board = find_board_fields(spec()["title"])
        field = next(f for f in board["fields"]["nodes"] if f.get("name") == EPIC_FIELD)
        options = epic_options(field["options"], *want)
        if options is not None:
            graphql("""mutation($input: UpdateProjectV2FieldInput!) {
                updateProjectV2Field(input: $input) { clientMutationId } }""",
                    input={"fieldId": field["id"], "singleSelectOptions": options})
            board = find_board_fields(spec()["title"])
    set_field(board, item_id, EPIC_FIELD, want[1])


def board_item(board, issue):
    """The issue's item on the board, adding it if needed. Returns (item id, status or None)."""
    for item in issue["projectItems"]["nodes"]:
        if item["project"]["id"] == board["id"]:
            return item["id"], status_on(board["id"], issue)
    try:
        item = graphql("""mutation($p: ID!, $c: ID!) { addProjectV2ItemById(
            input: {projectId: $p, contentId: $c}) { item { id } } }""", p=board["id"], c=issue["id"])
    except SystemExit as e:
        # Another writer (the issue-fields workflow on `opened`, or new-issue.py) added it a moment
        # earlier and GitHub refuses the second add (peras#30): use the item that now exists.
        if "already exists" not in str(e):
            raise
        for attempt in range(3):
            if attempt:
                time.sleep(1)
            fresh = load_issue(issue["repository"]["nameWithOwner"], issue["number"])
            for found in fresh["projectItems"]["nodes"]:
                if found["project"]["id"] == board["id"]:
                    return found["id"], status_on(board["id"], fresh)
        raise
    return item["addProjectV2ItemById"]["item"]["id"], None


def set_field(board, item_id, name, option):
    field = next(f for f in board["fields"]["nodes"] if f.get("name") == name)
    option_id = next(o["id"] for o in field["options"] if o["name"] == option)
    graphql("""mutation($p: ID!, $i: ID!, $f: ID!, $o: String!) { updateProjectV2ItemFieldValue(
        input: {projectId: $p, itemId: $i, fieldId: $f, value: {singleSelectOptionId: $o}}) {
        clientMutationId } }""", p=board["id"], i=item_id, f=field["id"], o=option_id)


def our_comment(repo, number):
    for c in gh(f"repos/{repo}/issues/{number}/comments?per_page=100") or []:
        if c["body"].startswith(MARKER):
            return c
    return None


def report(repo, number, issue, problems, what="issue"):
    labels = {l["name"] for l in issue["labels"]["nodes"]}
    comment = our_comment(repo, number)
    text = problems_text(problems, what)
    if problems:
        if LABEL not in labels:
            ensure_label(repo, LABEL)
            gh(f"repos/{repo}/issues/{number}/labels", "-X", "POST", body={"labels": [LABEL]})
    else:
        if LABEL in labels:
            gh(f"repos/{repo}/issues/{number}/labels/{LABEL}", "-X", "DELETE")
        if comment is None:
            return  # complete from the start: say nothing
    if comment is None:
        gh(f"repos/{repo}/issues/{number}/comments", "-X", "POST", body={"body": text})
    elif comment["body"] != text:
        gh(f"repos/{repo}/issues/comments/{comment['id']}", "-X", "PATCH", body={"body": text})


def sync(repo, number, forms, board):
    """Run every check on one issue and update the board, label and comment. Returns a summary."""
    issue = load_issue(repo, number)
    reopened = reopen_if_early(repo, number, issue)
    reclosed = not reopened and reclose_if_done(repo, number, issue)
    issue_type = (issue["issueType"] or {}).get("name")
    item_id, status = board_item(board, issue)
    names = {f.get("name") for f in board["fields"]["nodes"]}
    values = board_values(issue_type, issue["body"], forms, names)
    if status is None:
        values["Status"] = start_status(issue_type, issue["body"], issue["author"])
    for name, option in values.items():
        set_field(board, item_id, name, option)
    sync_epic(board, item_id, issue)
    problems = all_problems(issue, forms, values.get("Status", status))
    report(repo, number, issue, problems)
    set_reason(board, item_id, reason_on(board["id"], issue),
               want_reason(issue, bool(problems), values.get("Status", status)))
    want = epic_of(issue)
    return {"issue": f"{repo}#{number}", "type": issue_type, "set": values,
            "epic": want[1] if want else None,
            "reopened": reopened, "reclosed": reclosed, "problems": problems}


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    board = find_board_fields(spec()["title"])
    print(json.dumps(sync(sys.argv[1], sys.argv[2], load_forms(), board), indent=2))


if __name__ == "__main__":
    main()

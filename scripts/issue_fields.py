#!/usr/bin/env python3
"""Put an issue on the org board with its fields filled from the form answers, and flag anything
missing (design 6.6). The issue forms in .github/ISSUE_TEMPLATE/ are the only source of what is
required: an issue's type picks its form, every required question must have an answer, dropdown
answers must be one of the options, and a question labelled "(required when Kind is Bug fix)" is
required when that answer is given.

Dropdowns whose label is also a board field (Discipline, Phase, Priority, Audit, Severity) are
copied to the board. New items start in Todo, Decisions in Waiting for human. An issue with
problems gets the `needs-fields` label and one comment listing them; once fixed, the label goes
and the comment says so. The issue body is the source of truth: edit the answer there and the
board follows.

Links between issues are checked too (design 6.8): every issue has a parent unless it is top level
(an Intent; a parent of other issues, which is what phase steps and partner workstreams are; or the
"Deferred work" parent, handbook#83, top level even while empty), a Bug report not yet triaged,
or an issue a bot opened (the org's GitHub App, e.g. the settings-drift report); an issue whose
board Status is Blocked has a "blocked by" link; and a closed issue with open sub-issues is
reopened with a comment naming them. Board changes, links and closing send no event every repo's
caller listens to, so scripts/issue_sweep.py re-checks on a schedule.

Usage: scripts/issue_fields.py <owner/repo> <issue number>
In Actions it runs from the issue-fields workflow with an org GitHub App token in GH_TOKEN.
"""

import json
import os
import pathlib
import re
import subprocess
import sys

import yaml

from board import find_board_fields, graphql, spec
from rules import ROOT, gh

FORMS_DIR = ROOT / ".github" / "ISSUE_TEMPLATE"
LABEL = "needs-fields"
MARKER = "<!-- issue-fields -->"
REOPEN_MARKER = "<!-- issue-fields: reopened -->"
EMPTY = {"", "_No response_"}
REQUIRED_WHEN = re.compile(r"\(required when (.+?) is (.+?)\)")
START_STATUS = {"Decision": "Waiting for human"}  # everything else starts in Todo


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


# "Deferred work" (deferred findings and later-phase tasks): top level. Matched by number, so a
# rename doesn't break it.
DEFERRED = ("handbook", 83)
NO_PARENT_TYPES = {"Intent", "Bug"}  # Bug: a report needs no parent until it is triaged


def needs_parent(issue_type, sub_issues, repo="", number=0, by_bot=False):
    """False for issues design 6.8 lets stand alone; `repo` is the repo name without the owner."""
    return not (issue_type in NO_PARENT_TYPES or sub_issues or by_bot
                or (repo.lower(), int(number)) == DEFERRED)


def link_problems(issue_type, has_parent, sub_issues, status, blocked_by, repo="", number=0,
                  by_bot=False):
    """Problems with an issue's links (design 6.8, checks 3 and 4). `sub_issues` and `blocked_by`
    are counts."""
    problems = []
    if not has_parent and needs_parent(issue_type, sub_issues, repo, number, by_bot):
        problems.append("The issue has no parent. Add it as a sub-issue of the phase step, partner "
                        "workstream or feature it belongs to (deferred findings and later-phase "
                        "work go under handbook#83, Deferred work). Only intents, untriaged bugs and parents of other "
                        "issues stand alone.")
    if status == "Blocked" and not blocked_by:
        problems.append("Status is **Blocked** but nothing is linked as blocking it. Add a "
                        "\"blocked by\" link (Relationships, in the sidebar) to the issue it waits on.")
    return problems


def open_sub_issues(sub_issues):
    """'owner/repo#n' for each open sub-issue, from the GraphQL subIssues nodes."""
    return [f"{s['repository']['nameWithOwner']}#{s['number']}" for s in sub_issues
            if s["state"] == "OPEN"]


def reopen_text(open_subs):
    return (f"{REOPEN_MARKER}\nReopened: a parent can't close while its sub-issues are open "
            "(design 6.8). Close or move these first:\n\n" + "\n".join(f"- {s}" for s in open_subs))


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


# --- GitHub side -------------------------------------------------------------------------------

# What the checks read about an issue; issue_sweep.py reads the same for many issues at once.
ISSUE_FIELDS = """id number state body author { __typename login } issueType { name } labels(first: 50) { nodes { name } }
  repository { nameWithOwner }
  parent { number }
  subIssues(first: 50) { nodes { number state repository { nameWithOwner } } }
  blockedBy(first: 1) { totalCount }
  projectItems(first: 20) { nodes { id project { id }
    fieldValueByName(name: "Status") { ... on ProjectV2ItemFieldSingleSelectValue { name } } } }"""


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


def all_problems(issue, forms, status):
    issue_type = (issue["issueType"] or {}).get("name")
    return check(issue_type, issue["body"], forms) + link_problems(
        issue_type, issue["parent"] is not None, len(issue["subIssues"]["nodes"]), status,
        issue["blockedBy"]["totalCount"], issue["repository"]["nameWithOwner"].split("/")[1],
        issue["number"], (issue["author"] or {}).get("__typename") == "Bot")


def problems_text(problems):
    """The text of the issue's one comment for these problems."""
    if not problems:
        return f"{MARKER}\nAll required answers are filled in. Thanks."
    return (f"{MARKER}\nThis issue is missing something the board needs, so it is labelled "
            f"`{LABEL}` and kept out of planning views. To fix:\n\n"
            + "\n".join(f"- {p}" for p in problems))


def reopen_if_early(repo, number, issue):
    """Reopen a closed issue that still has open sub-issues. True if it did."""
    open_subs = open_sub_issues(issue["subIssues"]["nodes"])
    if issue["state"] != "CLOSED" or not open_subs:
        return False
    gh(f"repos/{repo}/issues/{number}", "-X", "PATCH", body={"state": "open"})
    gh(f"repos/{repo}/issues/{number}/comments", "-X", "POST",
       body={"body": reopen_text(open_subs)})
    issue["state"] = "OPEN"
    return True


def board_item(board, issue):
    """The issue's item on the board, adding it if needed. Returns (item id, status or None)."""
    for item in issue["projectItems"]["nodes"]:
        if item["project"]["id"] == board["id"]:
            return item["id"], status_on(board["id"], issue)
    item = graphql("""mutation($p: ID!, $c: ID!) { addProjectV2ItemById(
        input: {projectId: $p, contentId: $c}) { item { id } } }""", p=board["id"], c=issue["id"])
    return item["addProjectV2ItemById"]["item"]["id"], None


def set_field(board, item_id, name, option):
    field = next(f for f in board["fields"]["nodes"] if f.get("name") == name)
    option_id = next(o["id"] for o in field["options"] if o["name"] == option)
    graphql("""mutation($p: ID!, $i: ID!, $f: ID!, $o: String!) { updateProjectV2ItemFieldValue(
        input: {projectId: $p, itemId: $i, fieldId: $f, value: {singleSelectOptionId: $o}}) {
        clientMutationId } }""", p=board["id"], i=item_id, f=field["id"], o=option_id)


def ensure_label(repo):
    # gh label create --force creates or updates; quiet if it already exists as wanted.
    subprocess.run([os.environ.get("GH", "gh"), "label", "create", LABEL, "-R", repo, "--force",
                    "--color", "D93F0B", "--description",
                    "Missing or invalid form answers; kept out of planning views"],
                   check=True, capture_output=True)


def our_comment(repo, number):
    for c in gh(f"repos/{repo}/issues/{number}/comments?per_page=100") or []:
        if c["body"].startswith(MARKER):
            return c
    return None


def report(repo, number, issue, problems):
    labels = {l["name"] for l in issue["labels"]["nodes"]}
    comment = our_comment(repo, number)
    text = problems_text(problems)
    if problems:
        if LABEL not in labels:
            ensure_label(repo)
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
    issue_type = (issue["issueType"] or {}).get("name")
    item_id, status = board_item(board, issue)
    names = {f.get("name") for f in board["fields"]["nodes"]}
    values = board_values(issue_type, issue["body"], forms, names)
    if status is None:
        values["Status"] = START_STATUS.get(issue_type, "Todo")
    for name, option in values.items():
        set_field(board, item_id, name, option)
    problems = all_problems(issue, forms, values.get("Status", status))
    report(repo, number, issue, problems)
    return {"issue": f"{repo}#{number}", "type": issue_type, "set": values,
            "reopened": reopened, "problems": problems}


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    board = find_board_fields(spec()["title"])
    print(json.dumps(sync(sys.argv[1], sys.argv[2], load_forms(), board), indent=2))


if __name__ == "__main__":
    main()

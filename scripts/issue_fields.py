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

def load_issue(repo, number):
    owner, name = repo.split("/")
    data = graphql("""query($o: String!, $n: String!, $i: Int!) { repository(owner: $o, name: $n) {
        issue(number: $i) { id state body issueType { name } labels(first: 50) { nodes { name } }
          projectItems(first: 20) { nodes { id project { id }
            fieldValueByName(name: "Status") {
              ... on ProjectV2ItemFieldSingleSelectValue { name } } } } } } }""",
                   o=owner, n=name, i=int(number))
    return data["repository"]["issue"]


def board_item(board, issue):
    """The issue's item on the board, adding it if needed. Returns (item id, status or None)."""
    for item in issue["projectItems"]["nodes"]:
        if item["project"]["id"] == board["id"]:
            return item["id"], (item["fieldValueByName"] or {}).get("name")
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
    if problems:
        text = (f"{MARKER}\nThis issue is missing answers the board needs, so it is labelled "
                f"`{LABEL}` and kept out of planning views. Edit the issue to fix:\n\n"
                + "\n".join(f"- {p}" for p in problems))
        if LABEL not in labels:
            ensure_label(repo)
            gh(f"repos/{repo}/issues/{number}/labels", "-X", "POST", body={"labels": [LABEL]})
    else:
        text = f"{MARKER}\nAll required answers are filled in. Thanks."
        if LABEL in labels:
            gh(f"repos/{repo}/issues/{number}/labels/{LABEL}", "-X", "DELETE")
        if comment is None:
            return  # complete from the start: say nothing
    if comment is None:
        gh(f"repos/{repo}/issues/{number}/comments", "-X", "POST", body={"body": text})
    elif comment["body"] != text:
        gh(f"repos/{repo}/issues/comments/{comment['id']}", "-X", "PATCH", body={"body": text})


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    repo, number = sys.argv[1], sys.argv[2]
    forms = load_forms()
    issue = load_issue(repo, number)
    issue_type = (issue["issueType"] or {}).get("name")
    problems = check(issue_type, issue["body"], forms)

    want = spec()
    board = find_board_fields(want["title"])
    item_id, status = board_item(board, issue)
    names = {f.get("name") for f in board["fields"]["nodes"]}
    values = board_values(issue_type, issue["body"], forms, names)
    if status is None:
        values["Status"] = START_STATUS.get(issue_type, "Todo")
    for name, option in values.items():
        set_field(board, item_id, name, option)

    report(repo, number, issue, problems)
    print(json.dumps({"issue": f"{repo}#{number}", "type": issue_type, "set": values,
                      "problems": problems}, indent=2))


if __name__ == "__main__":
    main()

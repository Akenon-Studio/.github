#!/usr/bin/env python3
"""Create or update the org project board from rulesets/board.json: the project, its fields and
options, and the repos linked to it. Safe to re-run; it never deletes fields or items.

Usage: scripts/apply-board.py
Needs an org member logged in to gh with the project scope. The API cannot set a view's grouping,
sorting or board column field, so the script ends by listing those as steps to do by hand.
"""

import datetime

from board import (board_differences, fields_by_name, find_board, graphql, hand_steps,
                   linked_repos, spec, view_differences, views_by_name)
from rules import ORG


def create_board(title):
    org = graphql("query($org: String!) { organization(login: $org) { id } }", org=ORG)
    graphql("""mutation($owner: ID!, $title: String!) {
        createProjectV2(input: {ownerId: $owner, title: $title}) { projectV2 { url } } }""",
            owner=org["organization"]["id"], title=title)
    print(f"project '{title}': created")
    return find_board(title)


def options(want, live=None):
    """Spec options, keeping the IDs of options that already exist so item values survive."""
    ids = {o["name"]: o["id"] for o in (live or {}).get("options", [])}
    return [{**o, **({"id": ids[o["name"]]} if o["name"] in ids else {})} for o in want["options"]]


def iterations(field, count=4):
    """The current iteration and the next few, aligned to the spec's start date."""
    start = datetime.date.fromisoformat(field["start_date"])
    days = field["duration"]
    current = start + datetime.timedelta(days=(datetime.date.today() - start).days // days * days)
    return {"startDate": current.isoformat(), "duration": days, "iterations": [
        {"startDate": (current + datetime.timedelta(days=days * i)).isoformat(),
         "duration": days, "title": f"Iteration {i + 1}"} for i in range(count)]}


def apply_field(board, f):
    live = fields_by_name(board).get(f["name"])
    if live is None:
        extra = {}
        if f["type"] == "SINGLE_SELECT":
            extra["singleSelectOptions"] = options(f)
        if f["type"] == "ITERATION":
            extra["iterationConfiguration"] = iterations(f)
        graphql("""mutation($input: CreateProjectV2FieldInput!) {
            createProjectV2Field(input: $input) { clientMutationId } }""",
                input={"projectId": board["id"], "dataType": f["type"], "name": f["name"], **extra})
        print(f"field '{f['name']}': created")
    elif f["type"] == "SINGLE_SELECT":
        graphql("""mutation($input: UpdateProjectV2FieldInput!) {
            updateProjectV2Field(input: $input) { clientMutationId } }""",
                input={"fieldId": live["id"], "singleSelectOptions": options(f, live)})
        print(f"field '{f['name']}': options set")
    elif live["dataType"] != f["type"]:
        raise SystemExit(f"field '{f['name']}' is {live['dataType']}, expected {f['type']}; "
                         "fix it by hand (deleting a field deletes its values)")


def main():
    want = spec()
    board = find_board(want["title"]) or create_board(want["title"])
    graphql("""mutation($id: ID!, $desc: String!) {
        updateProjectV2(input: {projectId: $id, shortDescription: $desc}) { clientMutationId } }""",
            id=board["id"], desc=want["description"])
    for f in want["fields"]:
        apply_field(board, f)
    linked = {r["name"] for r in board["repositories"]["nodes"]}
    for repo in linked_repos(want["linked_repos"]):
        if repo not in linked:
            rid = graphql("query($o: String!, $n: String!) { repository(owner: $o, name: $n) { id } }",
                          o=ORG, n=repo)["repository"]["id"]
            graphql("""mutation($p: ID!, $r: ID!) {
                linkProjectV2ToRepository(input: {projectId: $p, repositoryId: $r}) { clientMutationId } }""",
                    p=board["id"], r=rid)
            print(f"repo '{repo}': linked")
    apply_views(find_board(want["title"]), want)
    board = find_board(want["title"])
    diffs = [d for d in board_differences(board, want) if d not in
             {f"view '{n}': {s}" for n, s in hand_steps(board, want)}]
    if diffs:
        raise SystemExit("Board still differs from rulesets/board.json:\n  - " + "\n  - ".join(diffs))
    print(f"Done: {board['url']}")
    steps = hand_steps(board, want)
    if steps:
        print("\nSet these by hand in each view's menu, then save the view:")
        for name, step in steps:
            print(f"  - {name}: {step}")


DEFAULT_VIEW = "View 1"  # the view GitHub makes with every new project


def apply_views(board, want):
    ids = {f["name"]: f["id"] for f in board["fields"]["nodes"] if "name" in f}
    have = views_by_name(board)
    for v in want["views"]:
        config = {"name": v["name"], "layout": v["layout"]}
        if v["fields"]:  # roadmaps take no column list
            config["configuration"] = {"visibleFieldIds": [ids[n] for n in v["fields"]]}
        if v["name"] in have:
            graphql("""mutation($input: UpdateProjectV2ViewInput!) {
                updateProjectV2View(input: $input) { clientMutationId } }""",
                    input={"viewId": have[v["name"]]["id"], "filter": v["filter"], **config})
        else:
            view = graphql("""mutation($input: CreateProjectV2ViewInput!) {
                createProjectV2View(input: $input) { projectV2View { id } } }""",
                           input={"projectId": board["id"], **config})
            graphql("""mutation($input: UpdateProjectV2ViewInput!) {
                updateProjectV2View(input: $input) { clientMutationId } }""",
                    input={"viewId": view["createProjectV2View"]["projectV2View"]["id"],
                           "filter": v["filter"]})
            print(f"view '{v['name']}': created")
    if DEFAULT_VIEW in have and DEFAULT_VIEW not in {v["name"] for v in want["views"]}:
        graphql("""mutation($id: ID!) { deleteProjectV2View(input: {viewId: $id}) { clientMutationId } }""",
                id=have[DEFAULT_VIEW]["id"])
        print(f"view '{DEFAULT_VIEW}': deleted (GitHub's default)")


if __name__ == "__main__":
    main()

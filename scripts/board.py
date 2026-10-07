"""The org project board (rulesets/board.json): find it, create it, and check it. Used by
apply-board.py and verify-settings.py."""

import datetime

from rules import ORG, gh, load, managed_repos

FIELDS = """
  fields(first: 50) { nodes {
    __typename
    ... on ProjectV2FieldCommon { id name dataType }
    ... on ProjectV2SingleSelectField { options { id name color description } }
    ... on ProjectV2IterationField { configuration {
      duration
      iterations { startDate duration title }
      completedIterations { startDate duration title }
    } }
  } }
  repositories(first: 50) { nodes { name } }
  views(first: 50) { nodes {
    id name layout filter
    fields(first: 50) { nodes { ... on ProjectV2FieldCommon { name } } }
    groupByFields(first: 5) { nodes { ... on ProjectV2FieldCommon { name } } }
    verticalGroupByFields(first: 5) { nodes { ... on ProjectV2FieldCommon { name } } }
    sortByFields(first: 5) { nodes { direction field { ... on ProjectV2FieldCommon { name } } } }
  } }
"""


def graphql(query, **variables):
    res = gh("graphql", body={"query": query, "variables": variables})
    if res.get("errors"):
        raise SystemExit(f"GraphQL error: {res['errors']}")
    return res["data"]


def spec():
    return load("board.json")


def find_board(title):
    """The open org project with this title, with its fields and linked repos, or None."""
    data = graphql(f"""query($org: String!) {{ organization(login: $org) {{
        projectsV2(first: 100) {{ nodes {{ id number title url closed shortDescription {FIELDS} }} }}
    }} }}""", org=ORG)
    boards = [p for p in data["organization"]["projectsV2"]["nodes"]
              if p["title"] == title and not p["closed"]]
    if len(boards) > 1:
        raise SystemExit(f"{len(boards)} open projects are called '{title}'; expected one")
    return boards[0] if boards else None


def fields_by_name(board):
    return {f["name"]: f for f in board["fields"]["nodes"] if "name" in f}


def linked_repos(want):
    return managed_repos() if want == "managed" else want


def iteration_starts(field):
    conf = field.get("configuration") or {}
    return [i["startDate"] for i in conf.get("iterations", []) + conf.get("completedIterations", [])]


def board_differences(board, want):
    """Every place the live board falls short of the spec."""
    if board is None:
        return [f"project '{want['title']}' does not exist"]
    diffs = []
    if board["shortDescription"] != want["description"]:
        diffs.append(f"description: expected {want['description']!r}")
    have = fields_by_name(board)
    for f in want["fields"]:
        live = have.get(f["name"])
        if live is None:
            diffs.append(f"field '{f['name']}' is missing")
            continue
        if live["dataType"] != f["type"]:
            diffs.append(f"field '{f['name']}': expected type {f['type']}, got {live['dataType']}")
            continue
        if f["type"] == "SINGLE_SELECT":
            strip = lambda opts: [{k: o[k] for k in ("name", "color", "description")} for o in opts]
            if strip(live["options"]) != f["options"]:
                diffs.append(f"field '{f['name']}': options are {[o['name'] for o in live['options']]}, "
                             f"expected {[o['name'] for o in f['options']]} (with colours and descriptions)")
        if f["type"] == "ITERATION":
            conf = live["configuration"]
            anchor = datetime.date.fromisoformat(f["start_date"])
            off = [s for s in iteration_starts(live)
                   if (datetime.date.fromisoformat(s) - anchor).days % f["duration"]]
            if conf["duration"] != f["duration"] or off:
                diffs.append(f"field '{f['name']}': expected {f['duration']}-day iterations aligned "
                             f"to {f['start_date']}, got duration {conf['duration']}, starts {off}")
    linked = {r["name"] for r in board["repositories"]["nodes"]}
    for repo in linked_repos(want["linked_repos"]):
        if repo not in linked:
            diffs.append(f"repo '{repo}' is not linked to the board")
    return diffs + view_differences(board, want) + [f"view '{n}': {d}" for n, d in hand_steps(board, want)]


def views_by_name(board):
    return {v["name"]: v for v in board["views"]["nodes"]}


def names(conn):
    return [n["name"] for n in conn["nodes"] if "name" in n]


def view_differences(board, want):
    """Differences in what apply-board.py sets: views, their layout, filter and columns."""
    diffs = []
    have = views_by_name(board)
    for v in want["views"]:
        live = have.get(v["name"])
        if live is None:
            diffs.append(f"view '{v['name']}' is missing")
            continue
        if live["layout"] != v["layout"]:
            diffs.append(f"view '{v['name']}': layout {live['layout']}, expected {v['layout']}")
        if (live["filter"] or "") != v["filter"]:
            diffs.append(f"view '{v['name']}': filter {live['filter']!r}, expected {v['filter']!r}")
        # The API sets which columns show, not their order (GitHub puts built-in fields first).
        if v["fields"] and sorted(names(live["fields"])) != sorted(v["fields"]):
            diffs.append(f"view '{v['name']}': columns {names(live['fields'])}, expected {v['fields']}")
    for extra in sorted(set(have) - {v["name"] for v in want["views"]}):
        diffs.append(f"view '{extra}' is not in board.json")
    return diffs


def hand_steps(board, want):
    """(view, step) pairs for settings the API cannot make: grouping, sorting, board columns."""
    steps = []
    have = views_by_name(board)
    for v in want["views"]:
        live = have.get(v["name"])
        if live is None:
            continue
        group = names(live["groupByFields"])
        if group != ([v["group_by"]] if "group_by" in v else []):
            steps.append((v["name"], f"Group by: {v.get('group_by', 'none')} (now {group or 'none'})"))
        column = names(live["verticalGroupByFields"])
        if "column" in v and column != [v["column"]]:
            steps.append((v["name"], f"Column field: {v['column']} (now {column or 'none'})"))
        sort = [[s["field"]["name"], s["direction"]] for s in live["sortByFields"]["nodes"]]
        if sort != ([v["sort"]] if "sort" in v else []):
            want_sort = " ".join(v["sort"]).replace("ASC", "ascending") if "sort" in v else "none"
            steps.append((v["name"], f"Sort by: {want_sort} (now {sort or 'none'})"))
    return steps

#!/usr/bin/env python3
"""Check every dependency's licence against rulesets/licences.json (design 6.4).

Usage: pnpm licenses list --json | scripts/licence_check.py [licences.json]
In Actions it runs as the `licences` job of the reusable security-scan workflow, in the calling
repo, after an install. A package passes if its licence expression can be satisfied from the
allowed list (one side of an OR, every side of an AND), or if an exception names it (a glob) and
its licence. A missing or unknown licence fails: someone has to look at it.
"""

import fnmatch
import json
import pathlib
import re
import sys

RULES = pathlib.Path(__file__).resolve().parent.parent / "rulesets" / "licences.json"
TOKEN = re.compile(r"\(|\)|\bOR\b|\bAND\b|[^\s()]+")


def satisfied(expression, ok):
    """Whether an SPDX expression can be met using only licences for which ok(id) is true.
    AND binds tighter than OR. A `WITH <exception>` clause is kept with its licence id."""
    tokens = TOKEN.findall(expression.replace(" WITH ", "-WITH-"))
    pos = 0

    def atom():
        nonlocal pos
        tok = tokens[pos]
        pos += 1
        if tok == "(":
            value = either()
            pos += 1  # the closing ")"
            return value
        return ok(tok.split("-WITH-")[0].rstrip("+"))

    def both():
        nonlocal pos
        value = atom()
        while pos < len(tokens) and tokens[pos] == "AND":
            pos += 1
            value = atom() and value
        return value

    def either():
        nonlocal pos
        value = both()
        while pos < len(tokens) and tokens[pos] == "OR":
            pos += 1
            value = both() or value
        return value

    try:
        return bool(tokens) and either() and pos == len(tokens)
    except IndexError:
        return False


def problems(listing, rules):
    """One line per package whose licence is not allowed, from `pnpm licenses list --json`."""
    allowed = set(rules["allowed"])
    found = []
    for licence, packages in sorted(listing.items()):
        for pkg in packages:
            name = pkg["name"]
            excepted = {e["licence"] for e in rules["exceptions"]
                        if fnmatch.fnmatchcase(name, e["package"])}
            if licence in excepted:
                continue
            if not satisfied(licence, lambda id_: id_ in allowed or id_ in excepted):
                versions = ", ".join(pkg.get("versions") or [])
                found.append(f"{name} {versions}: {licence}")
    return found


def main():
    rules = json.loads(pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else RULES).read_text())
    listing = json.load(sys.stdin)
    if not isinstance(listing, dict) or not all(isinstance(v, list) for v in listing.values()):
        print(f"::error::Not a `pnpm licenses list --json` listing: {json.dumps(listing)[:300]}")
        return 2
    found = problems(listing, rules)
    for line in found:
        print(f"::error::Licence not allowed: {line}")
    if found:
        print("\nA dependency's licence is outside rulesets/licences.json in the .github repo. "
              "Use another package, or add an exception there by PR, saying why (design 6.4).")
        return 1
    print("Every dependency's licence is allowed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Covered code changed but its doc didn't (design 6.5 item 3).

Every doc in the map names what it `covers` (scripts/docs_check.py). When a PR changes a file a doc
covers and leaves the doc alone, the PR either updates the doc or says, for that doc, that it is
still accurate, with a ticked line in its body the reviewer sees:

    - [x] Still accurate: docs/architecture/signals.md

Decision records, dated audits and the handbook's docs (which cover other repos) are left out.

Usage: scripts/docs_covered.py <root> --repo <name> --base <commit>   (reads PR_BODY; in Actions the
base is HEAD^1, the merge commit's base parent, which is current even when the event's base.sha is
stale)
In Actions it runs as the `docs-covered` job of the reusable pr-title workflow.
"""

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys

from docs_check import (HISTORY, RULES, expand_braces, front_matter, glob_re, matches, rules_for,
                        split_globs)

TICK = re.compile(r"^\s*[-*]\s*\[[xX]\]\s*Still accurate:\s*`?([^`\s]+)`?\s*$", re.M)


def changed_files(root, base):
    """Files the PR changes. --no-renames lists a moved file at both paths, so leaving a covered
    path counts too."""
    out = subprocess.run(["git", "diff", "--name-only", "--no-renames", "-z", f"{base}...HEAD"],
                         cwd=root, capture_output=True, text=True, check=True).stdout
    return [f for f in out.split("\0") if f]


def covering_docs(root, repo, rules):
    """{doc path: [globs]} for every doc in the map with a covers header (not history)."""
    r = rules_for(repo, rules)
    files = [f for f in subprocess.run(["git", "ls-files", "-z", "*.md"], cwd=root, capture_output=True,
                                       text=True, check=True).stdout.split("\0") if f]
    out = {}
    for path in files:
        if HISTORY.search(path) or not matches(path, r["docs"]) or matches(path, r["skip"]):
            continue
        text = (root / path).read_text(errors="ignore").lstrip("﻿").replace("\r\n", "\n")
        fm, _ = front_matter(text)
        if fm:
            out[path] = split_globs(fm.get("covers"))
    return out


def stale_docs(changed, docs):
    """Docs whose covered code changed while the doc itself didn't: {doc: [changed files]}."""
    changed_set = set(changed)
    found = {}
    for doc, globs in docs.items():
        if doc in changed_set:
            continue
        hits = [f for f in changed if not f.endswith(".md") and any(
            glob_re(e).match(f) or f.startswith(e.rstrip("/") + "/")
            for g in globs for e in expand_braces(g))]
        if hits:
            found[doc] = hits
    return found


def problems(stale, body):
    ticked = set(TICK.findall(body or ""))
    return {doc: files for doc, files in stale.items() if doc not in ticked}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--base", required=True)
    args = ap.parse_args()
    if args.repo == "handbook":
        print("The handbook's docs cover other repos' code, which a handbook PR doesn't change.")
        return 0
    root = pathlib.Path(args.root).resolve()
    stale = stale_docs(changed_files(root, args.base),
                       covering_docs(root, args.repo, json.loads(RULES.read_text())))
    left = problems(stale, os.environ.get("PR_BODY", ""))
    for doc, files in stale.items():
        state = "needs a look" if doc in left else "ticked still accurate"
        print(f"{doc}: covers {', '.join(files[:5])}{' …' if len(files) > 5 else ''} ({state})")
    if left:
        print("\nThis PR changes code these docs cover, and doesn't change the docs. Update each doc, or "
              "if it is still accurate, add this line to the PR body for it (design 6.5):\n")
        for doc in left:
            print(f"- [x] Still accurate: {doc}")
            print(f"::error::{doc} covers code this PR changes: update it, or tick "
                  f"\"Still accurate: {doc}\" in the PR body")
        return 1
    print("Every doc covering the changed code was updated or ticked still accurate." if stale
          else "No doc covers the code this PR changes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Check a CLAUDE.md (design 6.8): at most 60 lines; every rule ends with `enforced by: <check or
script>` or `not code because: <reason>`; and nothing it names is dead.

A rule is a list item or a paragraph (with its wrapped lines). Headings, blank lines, HTML comments
and fenced code blocks are not rules. Dead references:
- a path in backticks (it has a `/` or a file extension) that exists neither next to the file nor
  at the root; paths with placeholders (`<name>`, `*`) or spaces are not checked, nor are `~` and
  absolute paths when running in CI (they name a person's machine);
- `enforced by:` that names nothing in backticks, or names something that is neither an existing
  path nor a known check (a required check in rulesets/required-checks.json, or a job or workflow
  in this repo's or the .github repo's workflows);
- "design 6.8" or "section 6.8" with no such heading in handbook/design.md. Checked only where the
  design can be read (the handbook repo, and local runs such as the workspace CLAUDE.md); elsewhere
  the script says it skipped them.

Usage: scripts/claude_md.py <CLAUDE.md> [--root DIR] [--repo NAME] [--design design.md]
--root is where root-relative paths start (default: the file's folder); --repo picks that repo's
required checks; --design defaults to the first of design.md, handbook/design.md and
../handbook/design.md under the root that exists. Exits 1 if anything fails.
"""

import argparse
import json
import os
import pathlib
import re
import sys

import yaml

GITHUB_ROOT = pathlib.Path(__file__).resolve().parent.parent
MAX_LINES = 60
TAG = re.compile(r"\b(enforced by|not code because):\s*(\S.*)$", re.I | re.S)
LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
CODE = re.compile(r"`([^`\n]+)`")
EXTENSIONS = (".md", ".py", ".json", ".yml", ".yaml", ".sh", ".js", ".mjs", ".ts", ".tsx", ".txt",
              ".toml", ".swift")
SECTION_REF = re.compile(r"\b(?:design|section)s?\s+(\d+(?:\.\d+)*(?:(?:,\s*|\s+and\s+)\d+(?:\.\d+)*)*)",
                         re.I)
SECTION_HEADING = re.compile(r"^#+\s+(\d+(?:\.\d+)*)\.?\s", re.M)


def rules(text):
    """(first line number, text) of each rule: list items and paragraphs, wrapped lines joined."""
    blocks, current, fence = [], None, False
    text = re.sub(r"<!--.*?-->", lambda m: "\n" * m.group().count("\n"), text, flags=re.S)
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            fence = not fence
            current = None
            continue
        if fence or not line.strip() or line.lstrip().startswith("#"):
            current = None
            continue
        if LIST_ITEM.match(line) or current is None:
            current = [number, line.strip()]
            blocks.append(current)
        else:
            current[1] += " " + line.strip()
    return [tuple(b) for b in blocks]


def looks_like_path(token):
    return (("/" in token or token.endswith(EXTENSIONS)) and " " not in token
            and not re.search(r"[<>*{}$=]", token) and not token.startswith(("@", "http")))


def path_exists(token, here, root, in_ci):
    if token.startswith(("~", "/")):
        return True if in_ci else pathlib.Path(os.path.expanduser(token)).exists()
    token = token.split("#")[0]
    return (here / token).exists() or (root / token).exists()


def known_checks(repo, workflow_dirs):
    """Names a rule may give after `enforced by:` besides a path."""
    checks = json.loads((GITHUB_ROOT / "rulesets" / "required-checks.json").read_text())
    names = {n for key, value in checks.items() if not key.startswith("_")
             and (repo is None or key in ("all", repo)) for n in value}
    for directory in workflow_dirs:
        for path in sorted(pathlib.Path(directory).glob("*.yml")):
            workflow = yaml.safe_load(path.read_text()) or {}
            name = workflow.get("name", path.stem)
            names |= {name, path.name}
            for job_id, job in (workflow.get("jobs") or {}).items():
                names |= {job_id, f"{name} / {job_id}", job.get("name", job_id)}
    return names


def design_sections(design):
    return set(SECTION_HEADING.findall(design.read_text())) if design else None


def problems(text, here, root, checks, sections, in_ci=False):
    """What is wrong with a CLAUDE.md, as 'line N: ...' sentences. Empty means it passes."""
    found = []
    count = len(text.splitlines())
    if count > MAX_LINES:
        found.append(f"the file has {count} lines; the cap is {MAX_LINES}. Move lessons to a check, "
                     "the design or an issue first (design 6.8).")
    for number, rule in rules(text):
        tag = TAG.search(rule)
        if not tag:
            found.append(f"line {number}: ends with neither `enforced by:` nor `not code because:`.")
        elif tag.group(1).lower() == "enforced by":
            named = CODE.findall(tag.group(2))
            if not named:
                found.append(f"line {number}: `enforced by:` names no check or script in backticks.")
            for name in named:
                if name not in checks and not (looks_like_path(name)
                                               and path_exists(name, here, root, in_ci)):
                    found.append(f"line {number}: enforced by `{name}`, which is neither a known "
                                 "check nor an existing script.")
        for token in CODE.findall(rule):
            if looks_like_path(token) and not path_exists(token, here, root, in_ci) \
                    and not (tag and token in checks):
                found.append(f"line {number}: `{token}` does not exist.")
        if sections is not None:
            for group in SECTION_REF.findall(rule):
                for ref in re.split(r",\s*|\s+and\s+", group):
                    if ref not in sections:
                        found.append(f"line {number}: design section {ref} does not exist.")
    return list(dict.fromkeys(found))


def find_design(root):
    for candidate in (root / "design.md", root / "handbook" / "design.md",
                      root.parent / "handbook" / "design.md"):
        if candidate.is_file():
            return candidate
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("file", type=pathlib.Path)
    parser.add_argument("--root", type=pathlib.Path)
    parser.add_argument("--repo")
    parser.add_argument("--design", type=pathlib.Path)
    args = parser.parse_args()
    here = args.file.resolve().parent
    root = (args.root or here).resolve()
    design = args.design or find_design(root)
    checks = known_checks(args.repo, [root / ".github" / "workflows",
                                      GITHUB_ROOT / ".github" / "workflows"])
    found = problems(args.file.read_text(), here, root, checks, design_sections(design),
                     in_ci=bool(os.environ.get("CI")))
    if design is None:
        print(f"note: no handbook/design.md here, so design sections in {args.file} were not checked")
    for p in found:
        print(f"::error file={args.file}::{args.file}: {p}" if os.environ.get("CI")
              else f"{args.file}: {p}")
    if found:
        sys.exit(1)
    print(f"OK: {args.file}")


if __name__ == "__main__":
    main()

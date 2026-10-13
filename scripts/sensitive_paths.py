#!/usr/bin/env python3
"""The sensitive paths (design 6.2, 6.4), from rulesets/sensitive-paths.json: which of a change's
files are sensitive, and the CODEOWNERS block that gives 6.2's areas their sign-off.

peras/core/workspace/bin/review-branch.py loads this module from the workspace's .github checkout
to decide how many local review runs a branch needs; verify-settings.py checks each managed repo's
CODEOWNERS ends with the block.

Usage: scripts/sensitive_paths.py <repo>                 print the repo's CODEOWNERS block
       scripts/sensitive_paths.py <repo> <CODEOWNERS>    rewrite that file to end with it
"""

import json
import pathlib
import sys

RULES = pathlib.Path(__file__).resolve().parent.parent / "rulesets" / "sensitive-paths.json"
ORG = "akenon-studio"
BEGIN = "# Sensitive areas (design 6.2), generated from Akenon-Studio/.github's"
HEADER = (f"{BEGIN}\n# rulesets/sensitive-paths.json by scripts/sensitive_paths.py. Edit that file, not these\n"
          "# lines; they stay last, so they override the lines above.\n")


def areas(path=RULES):
    return json.loads(pathlib.Path(path).read_text())["areas"]


def patterns(repo, codeowners_only=False, path=RULES):
    """[(pattern, area)] for a repo, in the file's order, every repo's ('*') first within an area."""
    found = []
    for a in areas(path):
        if codeowners_only and not a["codeowners"]:
            continue
        own = a["paths"].get(repo, [])
        shared = [] if "*" in own else a["paths"].get("*", [])  # the whole repo covers the rest
        found += [(p, a) for p in shared + own]
    return found


def valid(pattern):
    """True for the patterns the file allows: '*', '/dir/' or '/path/file'."""
    return pattern == "*" or (pattern.startswith("/") and len(pattern) > 1 and "*" not in pattern
                              and "?" not in pattern and "[" not in pattern and "//" not in pattern)


def matches(pattern, file):
    """True if `pattern` covers `file` (a path from the repo's root, no leading slash)."""
    if pattern == "*":
        return True
    if pattern.endswith("/"):
        return file.startswith(pattern[1:])
    return file == pattern[1:]


def sensitive(repo, files, path=RULES):
    """{file: [area names]} for the files a sensitive pattern covers."""
    hits = {}
    for f in files:
        names = [a["area"] for p, a in patterns(repo, path=path) if matches(p, f)]
        if names:
            hits[f] = sorted(set(names), key=names.index)
    return hits


def block(repo, path=RULES):
    """The repo's generated CODEOWNERS block, or "" if none of its areas need a sign-off."""
    lines = [f"{p} @{ORG}/{a['team']}" for p, a in patterns(repo, codeowners_only=True, path=path)]
    return HEADER + "\n".join(lines) + "\n" if lines else ""


def without_block(text):
    """The CODEOWNERS text before the generated block, trailing blank lines dropped."""
    at = text.find(BEGIN)
    return (text if at < 0 else text[:at]).rstrip("\n")


def codeowners_problems(repo, text, path=RULES):
    """What is wrong with a repo's CODEOWNERS: it must end with exactly the generated block."""
    want = block(repo, path)
    if not want:
        return [] if BEGIN not in text else ["has a sensitive-areas block, but no area needs one"]
    if not text.endswith(want):
        return [f"doesn't end with the sensitive-areas block; run scripts/sensitive_paths.py {repo} "
                "<that file> to fix it"]
    return []


def write(repo, file, path=RULES):
    old = pathlib.Path(file).read_text() if pathlib.Path(file).exists() else ""
    before = without_block(old)
    pathlib.Path(file).write_text((before + "\n\n" if before else "") + block(repo, path))


def main(argv):
    if len(argv) == 1:
        print(block(argv[0]), end="")
        return 0
    if len(argv) == 2:
        write(*argv)
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

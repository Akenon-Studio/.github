#!/usr/bin/env python3
"""The gaming check (design 7.13, stage 6; 8.2 step 1): a PR must not pass by weakening its checks.

It reads the PR's diff and blocks the clear cases on lines the PR adds:
- a focused or skipped test (`.only`, `.skip`, `xit`, `xdescribe`, `xtest`, pytest or unittest skip);
- a test file deleted (a rename is fine);
- `@ts-ignore`, `@ts-nocheck`, `eslint-disable` or `biome-ignore`;
- `any` added to a type in non-test TypeScript.
Borderline cases are listed in the job summary for the reviewer, not blocked: `@ts-expect-error`,
`# type: ignore`, `# noqa`, a conditional skip, a test file that loses more assertions than it
gains, and bulk snapshot updates.

A line that must stay carries `gaming-check: <reason>` on the same line (after `--` in an eslint
comment), which the reviewer sees in the diff. Peras's own gate refuses that marker from agents.

Usage: scripts/gaming_check.py <base sha> [repo dir]   (diffs <base>...HEAD)
       scripts/gaming_check.py --diff <file>           (a saved `git diff -M --unified=0`)
In Actions it runs as the `gaming` job of the reusable pr-title workflow.
"""

import os
import re
import subprocess
import sys

MARKER = re.compile(r"gaming-check:\s*\S")
TEST_FILE = re.compile(r"(^|/)(tests?|__tests__|e2e)/|\.(test|spec)\.[cm]?[jt]sx?$|(^|/)test_[^/]*\.py$"
                       r"|_test\.py$")
CODE = re.compile(r"\.[cm]?[jt]sx?$|\.py$")
JS = re.compile(r"\.[cm]?[jt]sx?$")
TS = re.compile(r"\.[cm]?tsx?$")
SNAPSHOT = re.compile(r"\.snap$|(^|/)__snapshots__/")
# Built or vendored files; animations/<name>/index.js is the bundle built from that animation's src/.
SKIP_PATH = re.compile(r"(^|/)(node_modules|dist|out|build|\.witness)/|\.min\.js$|(^|/)pnpm-lock\.yaml$"
                       r"|^animations/[^/]+/index\.js$")

BLOCK_IN_TESTS = [
    (re.compile(r"\b(it|test|describe|context|suite|bench)\.only\s*\("), "focused test (.only) runs only itself"),
    (re.compile(r"\b(it|test|describe|context|suite|bench)\.skip\s*\("), "skipped test (.skip)"),
    (re.compile(r"(^|[^\w.])(xit|xdescribe|xtest|xcontext)\s*\("), "skipped test (x-prefixed)"),
    (re.compile(r"@pytest\.mark\.skip\b(?!if)|@unittest\.skip\b(?!If|Unless)|\bself\.skipTest\s*\("),
     "skipped test"),
]
BLOCK_IN_CODE = [
    (re.compile(r"@ts-ignore\b"), "@ts-ignore hides a type error"),
    (re.compile(r"@ts-nocheck\b"), "@ts-nocheck turns off type checking for the file"),
    (re.compile(r"eslint-disable"), "eslint-disable turns off a lint rule"),
    (re.compile(r"biome-ignore"), "biome-ignore turns off a lint rule"),
]
ANY = (re.compile(r":\s*any\b|\bas\s+any\b|<any>|\bany\[\]|Array<any>|Record<[^>]*,\s*any>"),
       "`any` loosens a type")
FLAG = [
    (re.compile(r"@ts-expect-error\b"), "@ts-expect-error"),
    (re.compile(r"#\s*type:\s*ignore\b"), "# type: ignore"),
    (re.compile(r"#\s*noqa\b(?!:\s*E402\b)"), "# noqa"),  # E402: an import after a sys.path change
    (re.compile(r"\.(skipIf|runIf)\s*\(|@pytest\.mark\.skipif\b|@unittest\.skip(If|Unless)\b"),
     "conditional skip"),
]
ASSERTION = re.compile(r"\bexpect\s*\(|\bassert\w*\b|\.should\b")
SNAPSHOT_FILES = 3


def run_diff(base, cwd):
    return subprocess.run(["git", "diff", "-M", "--unified=0", f"{base}...HEAD"], cwd=cwd,
                          capture_output=True, text=True, check=True).stdout


def parse(diff):
    """{path: {"added": [(line no, text)], "removed": [text], "deleted": bool}} from a diff."""
    files, cur, line_no = {}, None, 0
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            path = line.split(" b/", 1)[1] if " b/" in line else line.split()[-1]
            cur = files.setdefault(path, {"added": [], "removed": [], "deleted": False})
        elif cur is None:
            continue
        elif line.startswith("deleted file mode"):
            cur["deleted"] = True
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            line_no = int(m.group(1)) if m else 0
        elif line.startswith("+") and not line.startswith("+++"):
            cur["added"].append((line_no, line[1:]))
            line_no += 1
        elif line.startswith("-") and not line.startswith("---"):
            cur["removed"].append(line[1:])
    return files


def check(files):
    """(blocking, flagged): lists of 'path:line: reason' strings."""
    blocking, flagged = [], []
    snapshots = [p for p in files if SNAPSHOT.search(p)]
    if len(snapshots) >= SNAPSHOT_FILES:
        flagged.append(f"{len(snapshots)} snapshot files updated: check each change is intended")
    for path, f in sorted(files.items()):
        if SKIP_PATH.search(path) or not CODE.search(path):
            continue
        is_test = bool(TEST_FILE.search(path))
        if f["deleted"]:
            if is_test:
                blocking.append(f"{path}: test file deleted")
            continue
        rules = (BLOCK_IN_CODE if JS.search(path) else []) + (BLOCK_IN_TESTS if is_test else [])
        if TS.search(path) and not is_test:
            rules = rules + [ANY]
        for n, text in f["added"]:
            if MARKER.search(text):
                continue
            comment = text.lstrip().startswith(("//", "/*", "*", "#"))
            for pattern, why in rules:
                if pattern.search(text) and not (comment and why == ANY[1]):
                    blocking.append(f"{path}:{n}: {why}")
            for pattern, why in FLAG:
                if pattern.search(text):
                    flagged.append(f"{path}:{n}: {why}")
        if is_test:
            lost = (sum(bool(ASSERTION.search(t)) for t in f["removed"])
                    - sum(bool(ASSERTION.search(t)) for _, t in f["added"]))
            if lost > 0:
                flagged.append(f"{path}: {lost} fewer assertion line(s) than before")
    return blocking, flagged


def main(argv):
    if argv[:1] == ["--diff"]:
        diff = open(argv[1]).read()
    elif argv:
        diff = run_diff(argv[0], argv[1] if len(argv) > 1 else ".")
    else:
        print(__doc__)
        return 2
    blocking, flagged = check(parse(diff))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    lines = [f"### Gaming check: {len(blocking)} blocking, {len(flagged)} to look at\n"]
    lines += [f"- **blocks:** {b}" for b in blocking] + [f"- look at: {f}" for f in flagged]
    if summary:
        with open(summary, "a") as out:
            out.write("\n".join(lines) + "\n")
    for b in blocking:
        path, _, rest = b.partition(":")
        line, _, why = rest.partition(":") if rest[:1].isdigit() else ("", "", rest)
        loc = f"file={path}" + (f",line={line}" if line else "")
        print(f"::error {loc}::Gaming check:{why}")
    for f in flagged:
        print(f"::warning::Gaming check, look at: {f}")
    if blocking:
        print("\nFix it, or if the line must stay, add `gaming-check: <reason>` on it so the reviewer "
              "sees why (design 7.13, stage 6).")
        return 1
    print("Nothing weakens the checks." if not flagged else "Nothing blocking; see the warnings.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

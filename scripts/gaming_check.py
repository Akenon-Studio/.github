#!/usr/bin/env python3
"""The gaming check (design 7.13, stage 6; 8.2 step 1): a PR must not pass by weakening its checks.

It reads the PR's diff and blocks the clear cases on lines the PR adds:
- a focused or skipped test (`.only`, `.skip`, `xit`, `xdescribe`, `xtest`, pytest or unittest skip);
- a test file deleted (a rename is fine), unless every repo-local module it imports is deleted in
  the same PR: then the code and its tests went together, and it is listed for the reviewer instead.
  It still blocks when it imports nothing local, when an import can't be read or resolved (an alias,
  a computed `require`/`import()`/`importlib`, a Python module not found), when even one imported
  module survives, or when its content isn't in the diff (`--diff` with a diff made without it);
- `@ts-ignore`, `@ts-nocheck`, `eslint-disable` or `biome-ignore`;
- `any` added to a type in non-test TypeScript.
Borderline cases are listed in the job summary for the reviewer, not blocked: `@ts-expect-error`,
`# type: ignore`, `# noqa`, a conditional skip, a test file that loses more assertions than it
gains, a test file deleted with the modules it imports, and bulk snapshot updates.

A deleted test's imports are read from its removed lines (a deletion's whole content is in the
diff; with a base sha, a file missing there is read at the merge base). JS/TS: `import ... from`,
`export ... from`, `require()` and `import()` of a `./` or `../` path, with or without its extension
or `/index`. Python: `from .x import` resolved from the test's folder, and `import x`/`from x
import a, b` matched against the PR's deleted files (each of `a`, `b` that is a module must go, and
`x` itself when any isn't). Anything still at HEAD survives. A `node:` or standard-library name
isn't local; with a base sha, neither is a bare name the repo doesn't hold (not a file or folder,
for JS not a workspace package's `name` or a tsconfig `paths` alias), while with `--diff` (no repo
to look in) such a name can't be told from the repo's own and blocks.

A line that must stay carries `gaming-check: <reason>` on the same line (after `--` in an eslint
comment), which the reviewer sees in the diff. Peras's own gate refuses that marker from agents.

Usage: scripts/gaming_check.py <base sha> [repo dir]   (diffs <base>...HEAD)
       scripts/gaming_check.py --diff <file>           (a saved `git diff -M --unified=0`)
In Actions it runs as the `gaming` job of the reusable pr-title workflow.
"""

import json
import os
import pathlib
import posixpath
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

JS_EXT = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".mts", ".cts", ".tsx")
JS_IMPORTS = [
    re.compile(r"\bimport\s+(?:[\w*{}\s,$]+?\s+from\s+)?['\"]([^'\"]+)['\"]"),
    re.compile(r"\bexport\s+[\w*{}\s,$]+?\s+from\s+['\"]([^'\"]+)['\"]"),
    re.compile(r"\b(?:require|import)\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"),
]
# A require() or import() of anything but a plain string: what it loads can't be read.
JS_COMPUTED = re.compile(r"\b(?:require|import)\s*\(\s*(?!['\"])[^\s)]|\b(?:require|import)\s*\(\s*`")
PY_FROM = re.compile(r"^\s*from\s+(\.*)([\w.]*)\s+import\s+(\([^)]*\)|[^\n]+)", re.M)
PY_IMPORT = re.compile(r"^\s*import\s+([^\n]+)", re.M)
PY_COMPUTED = re.compile(r"\bimportlib\.import_module\s*\(|\b__import__\s*\(|\bimport_module\s*\(")


def run_diff(base, cwd):
    return subprocess.run(["git", "diff", "-M", "--unified=0", f"{base}...HEAD"], cwd=cwd,
                          capture_output=True, text=True, check=True).stdout


def read_deleted(files, base, cwd):
    """Fill in a deleted file's content from the merge base when the diff left it out."""
    missing = [p for p, f in files.items() if f["deleted"] and not f["removed"]]
    if not missing:
        return
    at = subprocess.run(["git", "merge-base", base, "HEAD"], cwd=cwd, capture_output=True,
                        text=True).stdout.strip()
    for path in missing:
        shown = subprocess.run(["git", "show", f"{at}:{path}"], cwd=cwd, capture_output=True,
                               text=True) if at else None
        if shown and shown.returncode == 0:
            files[path]["removed"] = shown.stdout.splitlines()


def repo_at_head(cwd):
    """What a deleted test's imports are resolved against: the repo's files at HEAD, and the bare
    names that are the repo's own JS code (each package.json's `name`, each tsconfig `paths` key)."""
    files = set(subprocess.run(["git", "ls-files"], cwd=cwd, capture_output=True, text=True,
                               check=True).stdout.splitlines())
    js_names = []
    for path in sorted(files):
        name = posixpath.basename(path)
        config = name == "package.json" or re.fullmatch(r"[jt]sconfig.*\.json", name)
        if SKIP_PATH.search(path) or not config:
            continue
        try:
            text = pathlib.Path(cwd, path).read_text()
        except OSError:
            continue
        if name == "package.json":
            try:
                js_names.append(str(json.loads(text).get("name") or ""))
            except (ValueError, AttributeError):
                continue
        elif m := re.search(r'"paths"\s*:\s*\{([^}]*)\}', text):
            js_names += [k.split("*")[0] for k in re.findall(r'"([^"]+)"\s*:\s*\[', m.group(1))]
    return {"files": files, "js_names": [n for n in js_names if n]}


def js_module(test, spec, paths):
    """The path in `paths` a relative import names, or None."""
    base = posixpath.normpath(posixpath.join(posixpath.dirname(test), spec))
    if base.startswith("../"):
        return None
    stem = base[: -len(ext)] if (ext := posixpath.splitext(base)[1]) in JS_EXT else base
    for c in [base] + [stem + e for e in JS_EXT] + [f"{base}/index{e}" for e in JS_EXT]:
        if c in paths:
            return c
    return None


def js_local_name(spec, names):
    """Whether a bare import names the repo's own code: a workspace package (`@scope/pkg`, or a
    file in it) or a tsconfig alias (`@app/*` is the prefix `@app/`)."""
    return any(spec.startswith(n) if n.endswith("/") else spec == n or spec.startswith(n + "/")
               for n in names)


def py_find(module, paths):
    """A path in `paths` that is the module `a.b` (a/b.py or a/b/__init__.py, under any folder)."""
    rel = module.replace(".", "/")
    for want in (f"{rel}.py", f"{rel}/__init__.py"):
        for p in paths:
            if p == want or p.endswith("/" + want):
                return p
    return None


def py_folder(module, paths):
    """Whether `a.b` is a folder of the repo (a namespace package such as a `scripts/` on sys.path)."""
    rel = module.replace(".", "/") + "/"
    return any(p.startswith(rel) or ("/" + rel) in p for p in paths)


def deleted_with_modules(test, lines, deleted, at_head):
    """(modules, None) when every repo-local module the deleted test imports is deleted too, or
    (None, why it still blocks). `at_head` is repo_at_head(), or None with --diff (no repo)."""
    if not lines:
        return None, "its content isn't in the diff"
    text = "\n".join(lines)
    head = at_head["files"] if at_head else set()
    found, unresolved = set(), []
    if JS.search(test):
        if JS_COMPUTED.search(text):
            return None, "a require() or import() it makes can't be read"
        for pattern in JS_IMPORTS:
            for spec in pattern.findall(text):
                if spec.startswith(("./", "../")) or spec in (".", ".."):
                    hit = js_module(test, spec, deleted)
                    (found.add(hit) if hit and not js_module(test, spec, head) else unresolved.append(spec))
                elif spec.startswith(("@/", "~/", "#", "/")):
                    unresolved.append(spec)  # a path alias or absolute path: not read here
                elif spec.startswith("node:"):
                    continue
                elif at_head is None or js_local_name(spec, at_head["js_names"]):
                    unresolved.append(spec)  # a workspace package or alias, or no repo to tell
    else:
        if PY_COMPUTED.search(text):
            return None, "an import it makes with importlib can't be read"
        here = posixpath.dirname(test)
        names = []
        for dots, module, what in PY_FROM.findall(text):
            imported = re.sub(r"#[^\n]*", "", what).strip().strip("()").replace("\n", ",").split(",")
            imported = [w.split(" as ")[0].strip() for w in imported]
            imported = [w for w in imported if w and w != "*"]
            if not dots:
                names.append((module, imported))
                continue
            folder = here
            for _ in range(len(dots) - 1):
                folder = posixpath.dirname(folder)
            if module:  # from .x import y: x itself
                options = [(f"{folder}/{module.replace('.', '/')}".lstrip("/"), f"{dots}{module}", False)]
            else:  # from . import x: the module x, or a name in the folder's __init__.py
                options = [(f"{folder}/{w}".lstrip("/"), f"{dots}{w}", True) for w in imported]
            for option, name, in_init in options:
                own = [f"{option}.py", f"{option}/__init__.py"]
                hit = next((c for c in own if c in deleted), None)
                if not hit and in_init and not any(c in head for c in own):
                    hit = next((c for c in [f"{folder}/__init__.py".lstrip("/")] if c in deleted), None)
                (found.add(hit) if hit and not any(c in head for c in own) else unresolved.append(name))
        for what in PY_IMPORT.findall(text):
            for w in what.split("#")[0].split(","):
                if w.strip():
                    names.append((w.split(" as ")[0].strip(), []))
        for module, imported in names:
            # Each imported name that is a module in its own right must go; the rest are names in
            # `module`, which must go itself.
            subs = [f"{module}.{w}" for w in imported]
            sub_modules = [m for m in subs if py_find(m, deleted) or py_find(m, head)]
            need_module = not subs or len(sub_modules) < len(subs)
            for m in sub_modules + ([module] if need_module else []):
                if py_find(m, head) or (m == module and py_folder(m, head) and not py_find(m, deleted)):
                    unresolved.append(m)  # still in the repo
                elif hit := py_find(m, deleted):
                    found.add(hit)
                elif m.split(".")[0] in sys.stdlib_module_names:
                    continue
                elif at_head is None:
                    unresolved.append(m)  # no repo to tell it from a package
    if unresolved:
        return None, f"{', '.join(sorted(set(unresolved)))} not shown deleted with it"
    if not found:
        return None, "it imports no repo module deleted with it"
    return sorted(found), None


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


def check(files, at_head=None):
    """(blocking, flagged): lists of 'path:line: reason' strings. `at_head` is repo_at_head() when
    there is a repo to look in (it tells a surviving module from a package)."""
    blocking, flagged = [], []
    deleted = {p for p, f in files.items() if f["deleted"]}
    snapshots = [p for p in files if SNAPSHOT.search(p)]
    if len(snapshots) >= SNAPSHOT_FILES:
        flagged.append(f"{len(snapshots)} snapshot files updated: check each change is intended")
    for path, f in sorted(files.items()):
        if SKIP_PATH.search(path) or not CODE.search(path):
            continue
        is_test = bool(TEST_FILE.search(path))
        if f["deleted"]:
            if is_test:
                modules, why = deleted_with_modules(path, f["removed"], deleted, at_head)
                if modules:
                    flagged.append(f"{path}: test file deleted with what it tests ({', '.join(modules)})")
                else:
                    blocking.append(f"{path}: test file deleted; {why}")
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
        files, at_head = parse(open(argv[1]).read()), None
    elif argv:
        cwd = argv[1] if len(argv) > 1 else "."
        files, at_head = parse(run_diff(argv[0], cwd)), repo_at_head(cwd)
        read_deleted(files, argv[0], cwd)
    else:
        print(__doc__)
        return 2
    blocking, flagged = check(files, at_head)
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

#!/usr/bin/env python3
"""The blocking docs checks (design 6.5 items 1 and 3), for one repo checked out at <root>:

1. Map: every Markdown file is in the repo's fixed map (rulesets/docs.json) or a recorded exception.
2. Header: every doc in the map has front matter naming its `owner` (a discipline) and what it
   `covers` (a glob or list of globs), and each glob matches a file in the repo (the handbook's
   `covers` names topics and other repos, so only its form is checked).
3. Dead references: a path in `code` (repo-relative, or relative to the doc) must exist; an
   ENV_VAR named in `code` must appear in the repo outside the docs (a `function()` that doesn't
   is only a warning: docs also name library functions). A path git ignores counts as existing
   (made at install), and a path whose first folder isn't in this repo is not checked. Skipped for
   decision records and dated audits (history, never edited: 6.5 item 5) and for the handbook,
   which names other repos' paths and planned layout.
4. Links: a relative Markdown link must point at a file that exists.

Usage: scripts/docs_check.py <root> --repo <name>
In Actions it runs as the `docs` job of the reusable pr-title workflow.
"""

import argparse
import json
import pathlib
import posixpath
import re
import subprocess
import sys

import yaml

RULES = pathlib.Path(__file__).resolve().parent.parent / "rulesets" / "docs.json"
HISTORY = re.compile(r"(^|/)decisions/|(^|/)legal/audits/")
FENCE = re.compile(r"^\s*(```|~~~)")
CODE = re.compile(r"`([^`\n]+)`")
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
ENV_VAR = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")
FUNCTION = re.compile(r"^(?:[A-Za-z_$][\w$]*\.)*([A-Za-z_$][\w$]*)\(\)$")


def glob_re(pattern):
    """A glob as a regex: * within a folder, ** across folders."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(f"^{out}$")


def matches(path, patterns):
    return any(glob_re(p).match(path) for p in patterns)


def rules_for(repo, rules):
    """The map for one repo: 'all' plus the repo's own entry."""
    own, base = rules.get(repo, {}), rules["all"]
    return {key: base.get(key, []) + own.get(key, []) for key in ("docs", "no_header", "skip")} | {
        "exceptions": {e["path"]: e for e in own.get("exceptions", [])}}


def front_matter(text):
    """The YAML header as a dict, or None if the file doesn't start with one."""
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end < 0:
        return None
    try:
        data = yaml.safe_load(text[4:end])
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def header_problems(path, text, owners, files, check_covers):
    fm = front_matter(text)
    if fm is None:
        return [f"{path}: no owner/covers header (front matter with `owner:` and `covers:`)"]
    found = []
    if fm.get("owner") not in owners:
        found.append(f"{path}: `owner` is {fm.get('owner')!r}; use one of {', '.join(owners)}")
    covers = fm.get("covers")
    globs = covers if isinstance(covers, list) else [g.strip() for g in str(covers or "").split(",")]
    globs = [g for g in globs if g]
    if not globs:
        found.append(f"{path}: `covers` is empty")
    elif check_covers:
        for g in globs:
            if not any(glob_re(g).match(f) or f.startswith(g.rstrip("/") + "/") for f in files):
                found.append(f"{path}: `covers` glob {g!r} matches no file")
    return found


def body_lines(text):
    """(line number, text) outside front matter and fenced code blocks."""
    lines = text.split("\n")
    start = 0
    if text.startswith("---\n"):
        end = next((i for i, l in enumerate(lines[1:], 1) if l.strip() == "---"), 0)
        start = end + 1
    fenced = False
    for n, line in enumerate(lines[start:], start + 1):
        if FENCE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            yield n, line


def looks_like_path(token):
    t = token.strip()
    if " " in t or "://" in t or t.startswith(("@", "~", "/", "$", "-", "<", "#", "./", "../")) or "=" in t:
        return False
    t = re.sub(r":\d+(-\d+)?$", "", t)
    # Only tokens with a folder: a bare name (`CLAUDE.md`) names a kind of file, not one path.
    return "/" in t and re.fullmatch(r"[\w.@*{}/-]+", t) is not None


def path_exists(root, doc_dir, token, files, dirs):
    t = re.sub(r":\d+(-\d+)?$", "", token.strip()).rstrip("/")
    t = re.sub(r"\{[^}]*\}", "*", t)  # docs write `src/{a,b}.ts` for several files
    first = t.split("/")[0]
    if first not in dirs and first not in files and not (pathlib.PurePosixPath(doc_dir) / first).as_posix() in dirs:
        return True  # not this repo's tree (another repo, a planned folder, the Linux kernel...)
    for base in ("", doc_dir):
        cand = str(pathlib.PurePosixPath(base) / t) if base else t
        cand = str(pathlib.PurePosixPath(cand))
        if "*" in cand:
            rx = glob_re(cand)
            if any(rx.match(f) for f in files) or any(rx.match(d) for d in dirs):
                return True
        elif cand in files or cand in dirs:
            return True
    return False


def ignored(root, token):
    """True for a path git ignores: generated or downloaded at install (`apps/desktop/models/`)."""
    t = re.sub(r":\d+(-\d+)?$", "", token.strip())
    return subprocess.run(["git", "check-ignore", "-q", "--no-index", t], cwd=root).returncode == 0


def code_problems(path, text, root, files, dirs, code_text):
    found = []
    doc_dir = str(pathlib.PurePosixPath(path).parent)
    for n, line in body_lines(text):
        for token in CODE.findall(line):
            token = token.strip()
            if ENV_VAR.match(token):
                if not re.search(rf"\b{re.escape(token)}\b", code_text):
                    found.append(f"{path}:{n}: env var `{token}` appears nowhere in the code")
            elif (m := FUNCTION.match(token)):
                # A warning, not a failure: docs also name library functions (three.js's wgslFn()).
                if not re.search(rf"\b{re.escape(m.group(1))}\b", code_text):
                    print(f"::warning::{path}:{n}: `{token}` appears nowhere in this repo's code")
            elif (looks_like_path(token) and not path_exists(root, doc_dir, token, files, dirs)
                  and not ignored(root, token)):
                found.append(f"{path}:{n}: path `{token}` doesn't exist")
    return found


def link_problems(path, text, files, dirs):
    found = []
    doc_dir = pathlib.PurePosixPath(path).parent
    for n, line in body_lines(text):
        for target in LINK.findall(line):
            if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("#"):
                continue
            rel = target.split("#")[0].split("?")[0]
            if not rel:
                continue
            resolved = posixpath.normpath(rel[1:] if rel.startswith("/") else str(doc_dir / rel))
            if resolved not in files and resolved not in dirs:
                found.append(f"{path}:{n}: link to `{target}` points at nothing")
    return found


def check(root, repo, rules):
    r = rules_for(repo, rules)
    files = [f for f in subprocess.run(["git", "ls-files"], cwd=root, capture_output=True,
                                        text=True, check=True).stdout.splitlines()
             if not matches(f, r["skip"])]
    dirs = {str(pathlib.PurePosixPath(f).parent) for f in files} | {
        "/".join(f.split("/")[:i]) for f in files for i in range(1, f.count("/") + 1)}
    code_text = "\n".join((root / f).read_text(errors="ignore") for f in files
                          if not f.endswith(".md") and (root / f).is_file()
                          and (root / f).stat().st_size < 2_000_000)
    found = []
    for path in (f for f in files if f.endswith(".md")):
        exception = r["exceptions"].get(path)
        in_map = matches(path, r["docs"]) or matches(path, r["no_header"]) or exception
        if not in_map:
            found.append(f"{path}: outside the docs map (design 6.5): move it into docs/"
                         "architecture, decisions or runbooks, or add an exception with the reason "
                         "to rulesets/docs.json in the .github repo")
            continue
        text = (root / path).read_text(errors="ignore")
        if matches(path, r["docs"]) and not matches(path, r["no_header"]) and not (
                exception and exception.get("no_header")):
            found += header_problems(path, text, rules["owners"], files, repo != "handbook")
        # The handbook speaks for every repo and for planned layout, so its paths can't resolve
        # here; decision records and dated audits are history (6.5 item 5).
        if repo != "handbook" and not HISTORY.search(path):
            found += code_problems(path, text, root, files, dirs, code_text)
        found += link_problems(path, text, files, dirs)
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--repo", required=True)
    args = ap.parse_args()
    found = check(pathlib.Path(args.root).resolve(), args.repo, json.loads(RULES.read_text()))
    for p in found:
        print(f"::error::{p}")
    if found:
        print(f"\n{len(found)} docs problem(s) (design 6.5). Fix the doc, or the code it names.")
        return 1
    print("Docs: every file is in the map, has its header, and names only things that exist.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

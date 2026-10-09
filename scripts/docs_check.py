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
import functools
import json
import pathlib
import posixpath
import re
import subprocess
import sys
import urllib.parse

import yaml

RULES = pathlib.Path(__file__).resolve().parent.parent / "rulesets" / "docs.json"
HISTORY = re.compile(r"(^|/)decisions/|(^|/)legal/audits/|(^|/)CHANGELOG\.md$")
UNCLOSED = "\0unclosed fence"  # body_lines() yields this when a code fence never closes
HEADER_END = re.compile(r"\n---[ \t]*(?:\n|$)")
FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
CODE = re.compile(r"`([^`\n]+)`")
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
BINARY = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".woff", ".woff2", ".ttf", ".otf", ".onnx",
          ".bin", ".glb", ".gltf", ".ply", ".pdf", ".zip", ".mp3", ".wav", ".mp4", ".ico", ".exr",
          ".hdr", ".ktx2", ".step", ".stl")
ENV_VAR = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")
FUNCTION = re.compile(r"^(?:[A-Za-z_$][\w$]*\.)*([A-Za-z_$][\w$]*)\(\)$")


@functools.lru_cache(maxsize=None)
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


def expand_braces(glob):
    """`src/{a,b}.ts` as ['src/a.ts', 'src/b.ts'] (nested braces expand too)."""
    m = re.search(r"\{([^{}]*)\}", glob)
    if not m:
        return [glob]
    return [g for alt in m.group(1).split(",")
            for g in expand_braces(glob[:m.start()] + alt + glob[m.end():])]


def matches(path, patterns):
    return any(glob_re(p).match(path) for p in patterns)


def rules_for(repo, rules):
    """The map for one repo: 'all' plus the repo's own entry."""
    own, base = rules.get(repo, {}), rules["all"]
    return {key: base.get(key, []) + own.get(key, [])
            for key in ("docs", "no_header", "skip", "known_names")} | {
        "exceptions": {e["path"]: e for e in own.get("exceptions", [])}}


def front_matter(text):
    """(header dict or None, error or None). None, None means the file has no header."""
    if not text.startswith("---\n"):
        return None, None
    end = HEADER_END.search(text, 3)
    if end is None:
        return None, "the header has no closing `---`"
    try:
        data = yaml.safe_load(text[4:end.start()]) or {}
    except yaml.YAMLError as e:
        return None, f"the header is not valid YAML ({str(e).splitlines()[0]}); quote globs, e.g. covers: \"**/*.md\""
    return (data, None) if isinstance(data, dict) else (None, "the header is not a set of fields")


def split_globs(value):
    """`covers` as a list of globs: a YAML list, or one string split on commas outside {braces}."""
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [g.strip() for g in re.split(r",(?![^{]*\})", str(value or "")) if g.strip()]


def header_problems(path, text, owners, files, check_covers):
    fm, error = front_matter(text)
    if error:
        return [f"{path}: {error}"]
    if fm is None:
        return [f"{path}: no owner/covers header (front matter with `owner:` and `covers:`)"]
    found = []
    if fm.get("owner") not in owners:
        found.append(f"{path}: `owner` is {fm.get('owner')!r}; use one of {', '.join(owners)}")
    globs = split_globs(fm.get("covers"))
    if not globs:
        found.append(f"{path}: `covers` is empty")
    elif check_covers:
        for g in globs:
            if not any(glob_re(e).match(f) or f.startswith(e.rstrip("/") + "/")
                       for e in expand_braces(g) for f in files):
                found.append(f"{path}: `covers` glob {g!r} matches no file")
    return found


def body_lines(text):
    """(line number, text) outside front matter and fenced code blocks."""
    start = 0
    if text.startswith("---\n") and (end := HEADER_END.search(text, 3)):
        start = text[:end.end()].count("\n")
    fence = None  # the opening marker; only the same character, at least as long, closes it
    for n, line in enumerate(text.split("\n")[start:], start + 1):
        m = FENCE.match(line)
        if m and fence is None:
            fence = m.group(1)
            continue
        if m and fence and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) \
                and not line.strip()[len(m.group(1)):].strip():
            fence = None
            continue
        if fence is None:
            yield n, line
    if fence is not None:
        yield 0, UNCLOSED


def looks_like_path(token):
    t = token.strip()
    if " " in t or "://" in t or t.startswith(("@", "~", "/", "$", "-", "<", "#", "./", "../")) or "=" in t:
        return False
    t = re.sub(r":\d+(-\d+)?$", "", t)
    # Only tokens with a folder: a bare name (`CLAUDE.md`) names a kind of file, not one path.
    return "/" in t and re.fullmatch(r"[\w.@*{},/-]+", t) is not None


def path_exists(root, doc_dir, token, files, dirs):
    """`files` and `dirs` are sets. A path under a top-level folder that no longer exists is not
    reported: it can't be told from another repo's or a planned folder's path."""
    t = re.sub(r":\d+(-\d+)?$", "", token.strip()).rstrip("/")
    m = re.search(r"\{([^}]*)\}", t)  # docs write `src/{a,b}.ts` for several files: each must exist
    if m:
        return all(path_exists(root, doc_dir, t[:m.start()] + alt + t[m.end():], files, dirs)
                   for alt in m.group(1).split(","))
    first = t.split("/")[0]
    if first not in dirs and first not in files and not (pathlib.PurePosixPath(doc_dir) / first).as_posix() in dirs:
        return True  # not this repo's tree (another repo, a planned folder, the Linux kernel...)
    for base in ("", doc_dir):
        cand = posixpath.normpath(posixpath.join(base, t) if base else t)
        if cand.startswith(".."):
            continue  # outside the repo
        if "*" in cand:
            rx = glob_re(cand)
            if any(rx.match(f) for f in files) or any(rx.match(d) for d in dirs):
                return True
        elif cand in files or cand in dirs:
            return True
    return False


def ignored(root, token, doc_dir=""):
    """True for a path git ignores, from the repo root or the doc's folder: made at install
    (`apps/desktop/models/`)."""
    t = re.sub(r":\d+(-\d+)?$", "", token.strip())
    bases = [t] + ([posixpath.join(doc_dir, t)] if doc_dir and doc_dir != "." else [])
    tries = [c for b in bases for c in (b.rstrip("/"), b.rstrip("/") + "/")]  # `models/` rules
    return any(subprocess.run(["git", "check-ignore", "-q", "--no-index", "--", c],
                              cwd=root).returncode == 0 for c in tries)


def code_problems(path, text, root, files, dirs, words, known=()):
    found = []
    doc_dir = str(pathlib.PurePosixPath(path).parent)
    for n, line in body_lines(text):
        for token in CODE.findall(line):
            token = token.strip()
            if ENV_VAR.match(token):
                if token not in words and token not in known:
                    found.append(f"{path}:{n}: env var `{token}` appears nowhere in the code (if it "
                                 "lives outside the code, e.g. a CI secret, add it to known_names "
                                 "in rulesets/docs.json)")
            elif (m := FUNCTION.match(token)):
                # A warning, not a failure: docs also name library functions (three.js's wgslFn()).
                if m.group(1) not in words:
                    print(f"::warning::{path}:{n}: `{token}` appears nowhere in this repo's code")
            elif (looks_like_path(token) and not path_exists(root, doc_dir, token, files, dirs)
                  and not ignored(root, token, doc_dir)):
                found.append(f"{path}:{n}: path `{token}` doesn't exist")
    return found


def link_problems(path, text, files, dirs, root=None):
    found = []
    doc_dir = pathlib.PurePosixPath(path).parent
    for n, line in body_lines(text):
        for target in LINK.findall(CODE.sub("", line)):
            if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("#"):
                continue
            rel = urllib.parse.unquote(target.split("#")[0].split("?")[0])
            if not rel:
                continue
            resolved = posixpath.normpath(rel[1:] if rel.startswith("/") else str(doc_dir / rel))
            if resolved not in files and resolved not in dirs and not (root and ignored(root, resolved)):
                found.append(f"{path}:{n}: link to `{target}` points at nothing")
    return found


def check(root, repo, rules):
    r = rules_for(repo, rules)
    files = [f for f in subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True,
                                       text=True, check=True).stdout.split("\0") if f]
    # (-z: unquoted names, so non-ASCII paths match; skipped files still count as existing)
    dirs = {str(pathlib.PurePosixPath(f).parent) for f in files} | {
        "/".join(f.split("/")[:i]) for f in files for i in range(1, f.count("/") + 1)}
    words = set()  # every identifier in the repo's text files outside the docs, read once
    for f in files:
        p = root / f
        if f.endswith(".md") or matches(f, r["skip"]) or f.lower().endswith(BINARY) or not p.is_file() or p.stat().st_size > 2_000_000:
            continue
        words |= set(re.findall(r"[A-Za-z_$][\w$]*", p.read_text(errors="ignore")))
    files = set(files)
    found = []
    for path in sorted(f for f in files if f.endswith(".md") and not matches(f, r["skip"])):
        exception = r["exceptions"].get(path)
        in_map = matches(path, r["docs"]) or matches(path, r["no_header"]) or exception
        if not in_map:
            found.append(f"{path}: outside the docs map (design 6.5): move it into docs/"
                         "architecture, decisions or runbooks, or add an exception with the reason "
                         "to rulesets/docs.json in the .github repo")
            continue
        if not (root / path).is_file():
            continue  # a dangling symlink or a submodule entry: nothing to read
        # A BOM or Windows line endings must not hide the header.
        text = (root / path).read_text(errors="ignore").lstrip("\ufeff").replace("\r\n", "\n")
        if (matches(path, r["docs"]) or exception) and not matches(path, r["no_header"]) and not (
                exception and exception.get("no_header")):
            found += header_problems(path, text, rules["owners"], files, repo != "handbook")
        if any(line == UNCLOSED for _, line in body_lines(text)):
            found.append(f"{path}: a code fence never closes, so the rest of the doc goes unchecked")
        # The handbook speaks for every repo and for planned layout, so its paths can't resolve
        # here; decision records and dated audits are history (6.5 item 5).
        if repo != "handbook" and not HISTORY.search(path):
            found += code_problems(path, text, root, files, dirs, words, set(r["known_names"]))
        links = link_problems(path, text, files, dirs, root)
        if HISTORY.search(path):
            # History is never edited, so a link that a later rename breaks is only a warning.
            for p in links:
                print(f"::warning::{p}")
        else:
            found += links
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

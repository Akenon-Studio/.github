#!/usr/bin/env python3
"""First-pass AI review of a PR (design 6.4: advisory; comments only, never approves).

Reads the PR's diff and the repo's CLAUDE.md, asks Claude for findings through the Claude API as structured JSON, and
posts them as one GitHub review with event COMMENT: a summary plus inline comments on lines the PR
adds. Uses the Claude API directly, not Claude Code: the plan's monthly API credits cover the API
but not Claude Code (platform.claude.com/docs/en/about-claude/api-credits-for-subscribers).

A push that leaves the PR's diff as it was (bringing `main` in without conflicts) gets no new
review: each review records the diff's `git patch-id`, which ignores line numbers (design 6.4).

Signs in with workload identity federation: each exchange presents a fresh GitHub OIDC token
(GitHub's tokens are single-use there), so no API key exists anywhere.

Usage: scripts/ai_review.py   (in Actions, as the `ai-review` reusable workflow; reads GITHUB_TOKEN,
GITHUB_REPOSITORY, PR_NUMBER, ACTIONS_ID_TOKEN_REQUEST_URL/_TOKEN and ANTHROPIC_FEDERATION_RULE_ID,
ANTHROPIC_ORGANIZATION_ID, ANTHROPIC_SERVICE_ACCOUNT_ID, ANTHROPIC_WORKSPACE_ID; optional
AI_REVIEW_MODEL)
       scripts/ai_review.py --dry-run <diff file>   (prints what it would post; needs the same
ANTHROPIC_* sign-in, or an ANTHROPIC_API_KEY for a local try)
"""

import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

MODEL = os.environ.get("AI_REVIEW_MODEL", "claude-sonnet-5-5")
MARKER = "<!-- ai-review -->"  # on every inline comment this script posts, to find them again
REVIEWER = "github-actions[bot]"  # who posts the reviews (the workflow's own token)
DIFF_MARKER = re.compile(r"<!-- ai-review-diff: ([0-9a-f]{40}) -->")  # in each review's body
MAX_DIFF_CHARS = 200_000
# Generated, vendored or lock files: reviewing them costs tokens and finds nothing.
SKIP_PATH = re.compile(r"(^|/)(node_modules|dist|out|build|\.witness)/|\.min\.js$|(^|/)pnpm-lock\.yaml$"
                       r"|^animations/[^/]+/index\.js$|\.snap$")

INSTRUCTIONS = """You are the first-pass reviewer for Akenon Studio (design 6.4). A person approves
every PR; you only comment. The repo's CLAUDE.md and the PR are below.

Look for, most serious first:
1. Bugs: wrong logic, unhandled errors and edge cases, race conditions.
2. Security: injection, secrets, unsafe input, auth mistakes, user data or audio leaving the device.
3. Architecture fit: code in the wrong layer or package, a new dependency between packages or apps,
   a provider SDK outside its adapter.
4. Wasted performance: allocations in render or audio loops, listeners or GPU resources never
   released, blocking calls on Electron's main process, a query per item, repeated work.
5. Docs: a paragraph the change makes wrong, duplicated text, a doc that isn't needed.

Comment only on lines the PR adds (the `+` lines; give the line number in the new file). Say what
goes wrong and the fix in under 80 words. At most 10 comments, the most important. No praise, no style nits a linter catches. If there is nothing
worth saying, return no comments and a one-line summary saying so."""

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "comments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "line": {"type": "integer"},
                    "body": {"type": "string"},
                },
                "required": ["path", "line", "body"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "comments"],
    "additionalProperties": False,
}


def split_diff(diff):
    """{path: text} per file of a unified diff, without skipped paths."""
    files, path, lines = {}, None, []
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if path:
                files[path] = "".join(lines)
            path, lines = line.rstrip().split(" b/", 1)[-1], []
        lines.append(line)
    if path:
        files[path] = "".join(lines)
    return {p: t for p, t in files.items() if not SKIP_PATH.search(p)}


def added_lines(file_diff):
    """Line numbers (in the new file) that a file's diff adds. Lines before the first hunk are the
    file header (`+++ b/...`); inside a hunk every `+` line is content, even `+++i;`."""
    added, n, in_hunk = set(), 0, False
    for line in file_diff.splitlines():
        if line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            n, in_hunk = (int(m.group(1)) if m else 0), True
        elif not in_hunk or line.startswith("\\"):  # header, or "\ No newline at end of file"
            continue
        elif line.startswith("+"):
            added.add(n)
            n += 1
        elif not line.startswith("-"):
            n += 1
    return added


def review_input(files):
    """The diff text sent to the model, cut to MAX_DIFF_CHARS; and the files left out."""
    text, left_out = "", []
    for path, t in files.items():
        if len(text) + len(t) > MAX_DIFF_CHARS:
            left_out.append(path)
        else:
            text += t
    return text, left_out


def to_github(review, files, left_out, already=frozenset()):
    """The GitHub review payload: inline comments that land on added lines; the rest go in the body.
    A (path, line) in `already` (commented on by an earlier review of this PR) is not repeated."""
    inline, stray, added = [], [], {p: added_lines(t) for p, t in files.items()}
    for c in review.get("comments", []):
        if (c.get("path"), c.get("line")) in already:
            continue
        if c.get("line") in added.get(c.get("path"), ()):
            inline.append({"path": c["path"], "line": c["line"], "side": "RIGHT",
                           "body": f"{c['body']}\n\n{MARKER}"})
        else:
            stray.append(f"- `{c.get('path')}:{c.get('line')}`: {c.get('body')}")
    body = "**AI review (advisory)**\n\n" + review.get("summary", "").strip()
    if stray:
        body += "\n\nAlso:\n" + "\n".join(stray)
    if left_out:
        body += "\n\nNot reviewed (diff too large): " + ", ".join(f"`{p}`" for p in left_out)
    return {"event": "COMMENT", "body": body, "comments": inline}


def fingerprint(files):
    """The `git patch-id --stable` of the reviewed files' diff: the same when only line numbers
    moved, as when `main` is brought in. None if git can't tell."""
    try:
        out = subprocess.run(["git", "patch-id", "--stable"], input="".join(files.values()),
                             capture_output=True, text=True, timeout=60).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return None
    return out[0] if out else None


def last_fingerprint(reviews):
    """The diff fingerprint in the latest of these reviews (oldest first) that has one. Only this
    workflow's own reviews count, so no one can silence the next review by posting the marker."""
    for review in reversed(reviews):
        if (review.get("user") or {}).get("login") != REVIEWER:
            continue
        m = DIFF_MARKER.search(review.get("body") or "")
        if m:
            return m.group(1)
    return None


def all_reviews(repo, number, token):
    reviews, page = [], 1
    while True:
        batch = github(f"repos/{repo}/pulls/{number}/reviews?per_page=100&page={page}", token) or []
        reviews += batch
        if len(batch) < 100:
            return reviews
        page += 1


def github(path, token, method="GET", body=None, accept="application/vnd.github+json"):
    req = urllib.request.Request(f"https://api.github.com/{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Accept": accept,
                                          "Content-Type": "application/json"})
    # nosemgrep: dynamic-urllib-use-detected -- always https://api.github.com/ plus a fixed path
    with urllib.request.urlopen(req, timeout=60) as res:
        raw = res.read().decode()
    return raw if accept.endswith("diff") else (json.loads(raw) if raw else None)


def earlier_comments(repo, number, token):
    """(path, line) of inline comments this script already posted on the PR (found by MARKER), so a
    new push does not repeat them. Outdated comments have no line and match nothing."""
    found, page = set(), 1
    while True:
        batch = github(f"repos/{repo}/pulls/{number}/comments?per_page=100&page={page}", token) or []
        found |= {(c["path"], c.get("line")) for c in batch if MARKER in (c.get("body") or "")}
        if len(batch) < 100:
            return found
        page += 1


def github_oidc_token():
    """A fresh GitHub Actions OIDC token for the Anthropic audience (single-use at the exchange)."""
    url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"] + "&audience=" + urllib.parse.quote(
        "https://api.anthropic.com", safe="")
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN']}"})
    # nosemgrep: dynamic-urllib-use-detected -- GitHub's own OIDC endpoint, from the runner
    with urllib.request.urlopen(req, timeout=30) as res:
        token = json.load(res)["value"]
    # The identity claims, not the token: what the federation rule matches on, for debugging it.
    payload = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    print("Signing in as:", {k: claims.get(k) for k in
                             ("sub", "repository", "event_name", "job_workflow_ref")})
    return token


def client():
    import anthropic
    if os.environ.get("ANTHROPIC_API_KEY"):  # a local try only; CI has no key
        return anthropic.Anthropic()
    return anthropic.Anthropic(credentials=anthropic.WorkloadIdentityCredentials(
        identity_token_provider=github_oidc_token,
        federation_rule_id=os.environ["ANTHROPIC_FEDERATION_RULE_ID"],
        organization_id=os.environ["ANTHROPIC_ORGANIZATION_ID"],
        service_account_id=os.environ["ANTHROPIC_SERVICE_ACCOUNT_ID"],
        workspace_id=os.environ["ANTHROPIC_WORKSPACE_ID"]))


def ask_claude(claude_md, title, description, diff_text):
    message = client().messages.create(
        model=MODEL, max_tokens=16000, system=INSTRUCTIONS,
        output_config={"format": {"type": "json_schema", "schema": REVIEW_SCHEMA}},
        messages=[{"role": "user", "content":
                   f"<claude_md>\n{claude_md}\n</claude_md>\n\n<pr_title>{title}</pr_title>\n"
                   f"<pr_description>\n{description}\n</pr_description>\n\n<diff>\n{diff_text}\n</diff>"}])
    print(f"Model {message.model}: {message.usage.input_tokens} in, {message.usage.output_tokens} out, "
          f"stop {message.stop_reason}")
    try:
        return json.loads(next(b.text for b in message.content if b.type == "text"))
    except (StopIteration, json.JSONDecodeError):
        return {"summary": f"The review was cut off ({message.stop_reason}) before it finished; "
                           "nothing to report from this run.", "comments": []}


def main(argv):
    if argv[:1] == ["--dry-run"]:
        files = split_diff(open(argv[1]).read())
        text, left_out = review_input(files)
        print(json.dumps(to_github(ask_claude("", "dry run", "", text), files, left_out), indent=2))
        return 0
    token, repo, number = (os.environ["GITHUB_TOKEN"], os.environ["GITHUB_REPOSITORY"],
                           os.environ["PR_NUMBER"])
    pr = github(f"repos/{repo}/pulls/{number}", token)
    try:
        diff = github(f"repos/{repo}/pulls/{number}", token, accept="application/vnd.github.diff")
    except urllib.error.HTTPError as e:
        if e.code != 406:
            raise
        diff = None  # GitHub refuses diffs over its size limits
    files = split_diff(diff) if diff is not None else {}
    if diff is not None and not files:
        print("Nothing to review: the PR changes only generated or lock files.")
        return 0
    fp = fingerprint(files) if files else None
    if fp and fp == last_fingerprint(all_reviews(repo, number, token)):
        print(f"The diff is unchanged since the last review (patch-id {fp}); nothing new to review.")
        return 0
    text, left_out = review_input(files)
    if not text:
        review = {"event": "COMMENT", "comments": [], "body": "**AI review (advisory)**\n\n"
                  "Not reviewed: the diff is too large. Split the PR, or ask a person to review it."}
    else:
        try:
            claude_md = open("CLAUDE.md").read()
        except OSError:
            claude_md = ""
        review = to_github(ask_claude(claude_md, pr["title"], pr.get("body") or "", text), files,
                           left_out, earlier_comments(repo, number, token))
    if fp:
        review["body"] += f"\n\n<!-- ai-review-diff: {fp} -->"
    post = {**review, "commit_id": pr["head"]["sha"]}
    try:
        github(f"repos/{repo}/pulls/{number}/reviews", token, method="POST", body=post)
    except urllib.error.HTTPError as e:
        if e.code != 422 or not review["comments"]:
            raise
        # GitHub refuses the whole review if one inline comment can't be placed: post them in the body
        lines = [f"- `{c['path']}:{c['line']}`: {c['body'].replace(MARKER, '').strip()}"
                 for c in review["comments"]]
        github(f"repos/{repo}/pulls/{number}/reviews", token, method="POST",
               body={**post, "comments": [], "body": review["body"] + "\n\n" + "\n".join(lines)})
    print(f"Posted a review with {len(review['comments'])} inline comment(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

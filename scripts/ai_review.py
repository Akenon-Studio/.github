#!/usr/bin/env python3
"""AI review of a PR (design 6.4: advisory; comments only, never approves).

Round one is the full review: the strongest model, more effort, no cap on findings, with the PR,
the repo's CLAUDE.md, the design sections that apply to the repo and every changed file in full.
Each finding carries an absolute severity (critical, major, minor, nit) and a confidence; critical
and major findings become review threads, minor ones and nits lines in the summary. Later rounds
review only the changes since the last review, with the earlier findings and the replies to them.
Uses the Claude API directly, not Claude Code: the plan's monthly API credits cover the API but not
Claude Code (platform.claude.com/docs/en/about-claude/api-credits-for-subscribers).

A push that leaves the PR's diff as it was (bringing `main` in without conflicts) gets no new
review: each review records the diff's `git patch-id --verbatim`, which ignores line numbers but not
whitespace (indentation is code in Python and YAML), with the repo's CLAUDE.md, so a new rule
brought in from `main` does get a review (design 6.4). A review cut off before it finished records
no fingerprint and fails the job, so a re-run or the next push reviews the diff again.

Signs in with workload identity federation: each exchange presents a fresh GitHub OIDC token
(GitHub's tokens are single-use there), so no API key exists anywhere.

Usage: scripts/ai_review.py   (in Actions, as the `ai-review` reusable workflow, run in a checkout
of the PR's head with its history; reads GITHUB_TOKEN, GITHUB_REPOSITORY, PR_NUMBER, DESIGN_FILE
(the handbook's design.md), ACTIONS_ID_TOKEN_REQUEST_URL/_TOKEN and ANTHROPIC_FEDERATION_RULE_ID,
ANTHROPIC_ORGANIZATION_ID, ANTHROPIC_SERVICE_ACCOUNT_ID, ANTHROPIC_WORKSPACE_ID; optional
AI_REVIEW_MODEL)
       scripts/ai_review.py --prompt <repo> <base ref> <design.md>   (in a checkout of the branch:
prints the round-one request, for a local review or a replay)
"""

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

MODEL = os.environ.get("AI_REVIEW_MODEL", "claude-opus-5-5")
EFFORT = "high"
MAX_TOKENS = 64000  # no cap on findings: the output limit is the only bound
MARKER = "<!-- ai-review -->"  # on every inline comment this script posts, to find them again
REVIEWER = "github-actions[bot]"  # who posts the reviews (the workflow's own token)
DIFF_MARKER = re.compile(r"<!-- ai-review-diff: ([0-9a-f]{40}) -->")  # in each review's body
# The tokens a review used, in its body, for the monthly scorecard's cost per PR (scripts/ai_scorecard.py)
USAGE_MARKER = re.compile(r"<!-- ai-review-usage: (\{[^<>]*\}) -->")
# On every thread: the reply the scorecard reads (design 6.4)
OUTCOME_ASK = ("Reply with the outcome first: **Fixed**, **Not an issue** (and why), or **Moved to** "
               "the issue's link; then resolve the thread.")
MAX_DIFF_CHARS = 200_000
# Everything the review reads (design, CLAUDE.md, diff, earlier rounds, files in full): about 120k
# tokens even for code at 3 characters a token, so with MAX_TOKENS of output it stays inside the
# model's 200k context window. The changed files in full get what is left; those that don't fit
# are left out, largest first, and the model is told which.
MAX_INPUT_CHARS = 360_000
MAX_EARLIER_CHARS = 50_000  # the earlier rounds' findings and replies, latest kept
MAX_BODY_CHARS = 60_000  # GitHub refuses a review body over 65,536 characters
MAX_DESCRIPTION_CHARS = 20_000
MAX_GENERATED = 200  # generated paths named in the request; the rest are counted (a vendored tree)
SECTION = re.compile(r"\d+(\.\d+)*")  # a design section number, the only design text posted
# Generated, vendored or lock files: reviewing them costs tokens and finds nothing.
SKIP_PATH = re.compile(r"(^|/)(node_modules|dist|out|build|\.witness)/|\.min\.js$|(^|/)pnpm-lock\.yaml$"
                       r"|^animations/[^/]+/index\.js$|\.snap$")
# The design sections each repo's review reads (design 6.4): structure, architecture, standards;
# Peras is also governed by 7.
DESIGN_SECTIONS = {"peras": ("4", "5", "6", "7")}
DEFAULT_SECTIONS = ("4", "5", "6")
SEVERITIES = ("critical", "major", "minor", "nit")
THREADS = {"critical", "major"}  # these become review threads; the rest go in the summary

INSTRUCTIONS = """You are the code reviewer for Akenon Studio (design 6.4). A person approves every
PR; you only comment. This is the only full review of this PR: later rounds see only the changes
made after it, so report every real problem now, however many there are. A problem found in a
later round in code you saw now is a miss; a finding that isn't a real problem costs the author a
round. Both count against you.

You get the repo's CLAUDE.md, the design sections that govern this repo, the PR's title and
description, every changed file in full as the PR leaves it (any left out for size are listed in
<files_omitted>: judge those from the diff alone), and the diff. Changed files that are generated
(lockfiles, build output) are not shown but are listed in <generated_files>: they did change, so
never report one as missing (unless it is marked deleted). The design is private and some
repos are public: cite design sections by number only, never quote the design's text.

Look for:
1. Bugs: wrong logic, unhandled errors and edge cases, race conditions, retries and timeouts that
   go wrong, state left behind on failure.
2. Security: injection, secrets, unsafe input, auth mistakes, a check that can be bypassed, user
   data or audio leaving the device.
3. Design fit: anything that breaks a rule in the design sections or CLAUDE.md, or puts code in the
   wrong layer or package; name the section (e.g. "6.2").
4. Wasted performance: allocations in render or audio loops, listeners or GPU resources never
   released, blocking calls on Electron's main process, a query per item, repeated work.
5. Docs: a paragraph the change makes wrong, duplicated text, a doc that isn't needed.
6. Tests: a changed behaviour no test checks, or a test that would pass with the change undone.

Leave out what the deterministic checks already enforce: formatting, lint, types, PR titles,
secret scanning, dependency audit, licences. No praise, no style preferences.

Severity is absolute. Judge each finding on its own against this scale, never against the other
findings: there is no quota, and ten critical findings are ten critical findings.
- critical: harm in normal use: a security hole, user data or audio leaving the device, data loss,
  a crash, a broken `main` or release, a required check that can be bypassed.
- major: wrong in a realistic case, or a design rule broken: wrong behaviour on a plausible input,
  a resource leak, waste on a hot path.
- minor: real but unlikely or small: a rare edge case, waste off the hot paths, a doc made slightly
  wrong.
- nit: polish.
A critical or major finding states its concrete failure in `failure`: the input or situation, what
goes wrong, and the result. If you can't state one, it is at most minor. Confidence is separate:
how sure you are the problem is real. A possible security hole you are unsure of stays critical,
with low confidence.

Each finding: the file, and a line the PR adds (a `+` line, numbered in the new file) where the
problem is; what goes wrong and the fix, in under 100 words; the design section it breaks, or "".
If there is nothing worth saying, return no findings and a one-line summary saying so."""

LATER_ROUND = """

This is a later round. You did the full review earlier; your findings and the replies to them are
below, then the changes pushed since (they can include changes brought in from `main`). Review
only those changes: report a problem they bring, or an earlier finding whose fix is wrong or
incomplete. Don't repeat earlier findings, and don't raise findings about code the earlier review
saw unchanged. Use the same scale; raise an earlier finding's severity only if the new code made
it worse."""

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
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "design": {"type": "string"},
                    "failure": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["path", "line", "severity", "confidence", "design", "failure", "body"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "comments"],
    "additionalProperties": False,
}


class CutOff(Exception):
    """The model stopped before it finished the review. `usage` is what it spent, when it ran."""
    usage = None


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


def generated_paths(diff):
    """The changed paths split_diff leaves out (lockfiles, build output), so the model knows they
    changed (.github#114); a deleted one says so."""
    out, path, deleted = [], None, False
    for line in (diff or "").splitlines() + ["diff --git a/ b/"]:
        if line.startswith("diff --git "):
            if path and SKIP_PATH.search(path):
                out.append(f"{path} (deleted)" if deleted else path)
            path, deleted = line.rstrip().split(" b/", 1)[-1], False
        elif line.startswith("deleted file mode"):
            deleted = True
    if len(out) > MAX_GENERATED:  # bounded, so it can't push the request past the window
        out = out[:MAX_GENERATED] + [f"... and {len(out) - MAX_GENERATED} more"]
    return out


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


def design_sections(design, repo):
    """The top-level sections of design.md that govern `repo` (DESIGN_SECTIONS), as one text."""
    want = DESIGN_SECTIONS.get(repo, DEFAULT_SECTIONS)
    parts = re.split(r"(?m)^(?=## )", design)
    return "".join(p for p in parts if re.match(r"## (\d+)\.", p) and
                   re.match(r"## (\d+)\.", p).group(1) in want)


def full_files(paths, root=".", budget=MAX_INPUT_CHARS):
    """({path: text} of the changed files as the PR leaves them, [paths left out for size]):
    largest left out first past `budget` characters; deleted and binary files are skipped."""
    found, omitted = {}, []
    top = os.path.realpath(root)
    for p in paths:
        path = os.path.join(root, p)
        # A symlink in the PR could point at the design or a runner file: only real files in the repo
        if os.path.islink(path) or not os.path.realpath(path).startswith(top + os.sep):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                found[p] = f.read()
        except (OSError, UnicodeDecodeError):
            continue
    while found and sum(map(len, found.values())) > budget:
        largest = max(found, key=lambda k: len(found[k]))
        del found[largest]
        omitted.append(largest)
    return found, omitted


NO_DESIGN = "(The design could not be read for this run: judge design fit from CLAUDE.md.)"


def read_design(path, repo):
    """The design sections for `repo`, or NO_DESIGN when the file is missing or has none."""
    try:
        with open(path) as f:
            return design_sections(f.read(), repo) or NO_DESIGN
    except (TypeError, OSError):  # TypeError: no path given
        return NO_DESIGN


def tagged(tag, text):
    return f"<{tag}>\n{text}\n</{tag}>"


def request(claude_md, design, title, description, files, diff_text, later=None, omitted=(),
            history="", generated=()):
    """(system, user) for the Messages API. The system holds what is the same for every round and
    PR of a repo (instructions, CLAUDE.md, design), so it is cached; `later` is (earlier findings
    and replies, changes since the last review) for a later round, which says so in the user
    message to keep the cached prefix the same. A later round's `files` are those the changes
    touch, and it gets no `diff_text`. `history` is the earlier rounds for a full review that isn't
    the first (CLAUDE.md changed, or too much changed), so it doesn't repeat what was settled."""
    system = [{"type": "text", "text": INSTRUCTIONS},
              {"type": "text", "text": tagged("claude_md", claude_md) + "\n\n" + tagged("design", design),
               "cache_control": {"type": "ephemeral", "ttl": "1h"}}]
    user = ((LATER_ROUND.strip() + "\n\n" if later else "") + f"<pr_title>{title}</pr_title>\n" + tagged("pr_description", description) + "\n\n" +
            "\n".join(f'<file path="{p}">\n{t}\n</file>' for p, t in files.items()) + "\n\n" +
            (tagged("files_omitted", "\n".join(omitted)) + "\n\n" if omitted else "") +
            (tagged("generated_files", "\n".join(generated)) + "\n\n" if generated else "") +
            (tagged("diff", diff_text) if diff_text else ""))
    if later:
        user += "\n\n" + tagged("earlier_review", later[0]) + "\n\n" + tagged("changes_since", later[1])
    elif history:
        user += ("\n\nThis PR was reviewed before; those findings and the replies are below. Don't "
                 "repeat them or findings the replies settled.\n" + tagged("earlier_review", history))
    return system, user


def heading(c):
    """`**Major** · high confidence · design 6.2` for a finding. The design field is posted only as
    a section number: the design is private and some repos are public."""
    parts = [f"**{c.get('severity', 'minor').capitalize()}**", f"{c.get('confidence', 'high')} confidence"]
    section = (c.get("design") or "").strip().removeprefix("design ").strip()
    if SECTION.fullmatch(section):
        parts.append(f"design {section}")
    return " · ".join(parts)


def to_github(review, files, left_out, already=frozenset(), round_no=1):
    """The GitHub review payload. Critical and major findings on lines the PR adds are inline
    threads; the rest go in the body, grouped by severity. A (path, line) in `already` (commented
    on by an earlier review of this PR) is not repeated."""
    inline, rest, added = [], {s: [] for s in SEVERITIES}, {p: added_lines(t) for p, t in files.items()}
    for c in review.get("comments", []):
        if (c.get("path"), c.get("line")) in already:
            continue
        severity = c.get("severity") if c.get("severity") in SEVERITIES else "minor"
        failure = (c.get("failure") or "").strip()
        text = c.get("body", "").strip() + (f"\n\n**Fails when:** {failure}" if failure and severity in THREADS else "")
        if severity in THREADS and c.get("line") in added.get(c.get("path"), ()):
            inline.append({"path": c["path"], "line": c["line"], "side": "RIGHT",
                           "body": f"{heading(c)}\n\n{text}\n\n{OUTCOME_ASK}\n\n{MARKER}"})
        else:
            rest[severity].append(f"- `{c.get('path')}:{c.get('line')}` ({heading(c)}): {text}")
    body = f"**AI review (advisory), round {round_no}**\n\n" + review.get("summary", "").strip()
    titles = {"critical": "Critical, not on a changed line", "major": "Major, not on a changed line",
              "minor": "Minor", "nit": "Nits"}
    left = 0
    for severity in SEVERITIES:  # most severe first, so a cut drops the least severe
        if rest[severity]:
            body += f"\n\n{titles[severity]}:"
        for item in rest[severity]:
            if len(body) + len(item) > MAX_BODY_CHARS:
                left += 1
            else:
                body += "\n" + item
    if left:
        body += f"\n\n{left} more finding(s) left out: the review body has a size limit."
    if left_out:
        body += "\n\nNot reviewed (diff too large): " + ", ".join(f"`{p}`" for p in left_out)
    return {"event": "COMMENT", "body": body, "comments": inline}


def fingerprint(files, claude_md=""):
    """What the review read, as 40 hex digits: the `git patch-id --verbatim` of the reviewed files'
    diff (the same when only line numbers moved, as when `main` is brought in; whitespace counts)
    and the repo's CLAUDE.md. None if git can't tell."""
    try:
        out = subprocess.run(["git", "patch-id", "--verbatim"], input="".join(files.values()),
                             capture_output=True, text=True, timeout=60).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return None
    # nosemgrep: insecure-hash-algorithm-sha1 -- an identity fingerprint compared with our own earlier one, not security
    return hashlib.sha1(f"{out[0]}\n{claude_md}".encode()).hexdigest() if out else None


def strip_markers(text):
    """Text without fingerprint or usage markers, removed until none is left (one pass could join
    the halves of a marker split around another into a new one)."""
    while True:
        stripped = USAGE_MARKER.sub("", DIFF_MARKER.sub("", text))
        if stripped == text:
            return text
        text = stripped


def own_reviews(reviews):
    """This workflow's finished reviews (they carry a fingerprint), oldest first. Only its own count,
    so no one can silence the next review by posting the marker."""
    return [r for r in reviews if (r.get("user") or {}).get("login") == REVIEWER
            and DIFF_MARKER.search(r.get("body") or "")]


def last_fingerprint(reviews):
    """The diff fingerprint in the latest of these reviews (oldest first) that has one."""
    done = own_reviews(reviews)
    # ours is appended last; the model's text is stripped of any (main)
    return DIFF_MARKER.findall(done[-1]["body"])[-1] if done else None


def changes_since(commit, paths):
    """`git diff` of the PR's files from an earlier reviewed commit to the checkout; None when that
    commit isn't in the history (the branch was rewritten), so the round is a full review."""
    try:
        run = subprocess.run(["git", "diff", commit, "HEAD", "--", *paths],
                             capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return run.stdout if run.returncode == 0 else None


THREADS_QUERY = """query($owner: String!, $name: String!, $n: Int!, $after: String) {
  repository(owner: $owner, name: $name) { pullRequest(number: $n) {
    reviewThreads(first: 50, after: $after) { pageInfo { hasNextPage endCursor } nodes {
      isResolved path line originalLine
      comments(first: 20) { nodes { author { login } body } } } } } } }"""


def earlier_review(repo, number, token, reviews):
    """The earlier rounds as text for the model: each review's summary, then each of its threads
    with the replies and whether it was resolved."""
    owner, name = repo.split("/")
    threads, after = [], None
    while True:
        data = graphql(THREADS_QUERY, token, owner=owner, name=name, n=int(number), after=after)
        page = data["repository"]["pullRequest"]["reviewThreads"]
        threads += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    out = [strip_markers(r["body"]).strip() for r in reviews]
    for t in threads:
        comments = t["comments"]["nodes"]
        if not comments or MARKER not in (comments[0]["body"] or ""):
            continue
        lines = [f"Thread on {t['path']}:{t.get('line') or t.get('originalLine')}"
                 f" ({'resolved' if t['isResolved'] else 'open'}):"]
        for c in comments:
            who = (c.get("author") or {}).get("login", "?")
            lines.append(f"[{who}] {c['body'].replace(MARKER, '').replace(OUTCOME_ASK, '').strip()}")
        out.append("\n".join(lines))
    return "\n\n---\n\n".join(out)[-MAX_EARLIER_CHARS:]  # the latest rounds, if it's long


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


def graphql(query, token, **variables):
    out = github("graphql", token, method="POST", body={"query": query, "variables": variables})
    if out.get("errors"):
        raise RuntimeError(f"GitHub GraphQL: {out['errors']}")
    return out["data"]


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


def parse(stop_reason, texts):
    """The review from the model's text blocks; CutOff unless it finished with valid JSON."""
    if stop_reason != "end_turn":
        raise CutOff(stop_reason)
    try:
        return json.loads("".join(texts))
    except json.JSONDecodeError:
        raise CutOff("unreadable output") from None


def ask_claude(system, user):
    import anthropic
    try:
        # Streamed: a long review can outlast a plain request's timeout
        with client().messages.stream(
                model=MODEL, max_tokens=MAX_TOKENS, system=system, thinking={"type": "adaptive"},
                output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": REVIEW_SCHEMA}},
                messages=[{"role": "user", "content": user}]) as stream:
            message = stream.get_final_message()
    except anthropic.BadRequestError as e:
        if "too long" not in str(e):
            raise  # a bad parameter: the log shows it, and a re-run won't help
        raise CutOff("the PR is too large for one review") from None
    u = message.usage
    print(f"Model {message.model}: {u.input_tokens} in (+{u.cache_read_input_tokens or 0} cached, "
          f"+{u.cache_creation_input_tokens or 0} written to cache), {u.output_tokens} out, "
          f"stop {message.stop_reason}")
    usage = {"model": message.model, "requested": MODEL, "input": u.input_tokens, "cache_read": u.cache_read_input_tokens or 0,
             "cache_write": u.cache_creation_input_tokens or 0, "output": u.output_tokens}
    try:
        return parse(message.stop_reason, [b.text for b in message.content if b.type == "text"]), usage
    except CutOff as e:
        e.usage = usage  # a cut-off review still cost this
        raise


def usage_marker(usage):
    return f"\n\n<!-- ai-review-usage: {json.dumps(usage)} -->" if usage else ""


def prompt_main(repo, base, design_path):
    """Print the round-one request for the checked-out branch against `base` (local review, replay)."""
    diff = subprocess.run(["git", "diff", f"{base}...HEAD"], capture_output=True, text=True, check=True).stdout
    files = split_diff(diff)
    text, _ = review_input(files)
    claude_md = open("CLAUDE.md").read() if os.path.exists("CLAUDE.md") else ""
    title = subprocess.run(["git", "log", "-1", "--format=%s"], capture_output=True, text=True).stdout.strip()
    whole, omitted = full_files(files, budget=MAX_INPUT_CHARS - len(text))
    system, user = request(claude_md, design_sections(open(design_path).read(), repo), title, "",
                           whole, text, omitted=omitted, generated=generated_paths(diff))
    print(json.dumps({"system": system, "user": user}))
    return 0


def main(argv):
    if argv[:1] == ["--prompt"]:
        return prompt_main(*argv[1:4])
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
    try:
        claude_md = open("CLAUDE.md").read()
    except OSError:
        claude_md = ""
    reviews = all_reviews(repo, number, token)
    fp = fingerprint(files, claude_md) if files else None
    if fp and fp == last_fingerprint(reviews):
        print(f"The diff and CLAUDE.md are unchanged since the last review ({fp}); nothing new to review.")
        return 0
    done = own_reviews(reviews)
    text, left_out = review_input(files)
    usage = None
    if not text:
        review = {"event": "COMMENT", "comments": [], "body": "**AI review (advisory)**\n\n"
                  "Not reviewed: the diff is too large. Split the PR, or ask a person to review it."}
    else:
        design = read_design(os.environ.get("DESIGN_FILE"), repo.split("/")[1])
        since = changes_since(done[-1]["commit_id"], list(files)) if done else None
        # A full round instead when a new CLAUDE.md rule applies to the whole PR, or the changes
        # are as big as a diff can be
        if since is not None and (changes_since(done[-1]["commit_id"], ["CLAUDE.md"])
                                  or len(since) > MAX_DIFF_CHARS):
            since = None
        history = earlier_review(repo, number, token, done) if done else ""
        if since is not None:  # a later round: the changes, and the files they touch in full
            later, shown, paths = (history, since), "", list(split_diff(since))
        else:
            later, shown, paths = None, text, list(files)
        description = (pr.get("body") or "")[:MAX_DESCRIPTION_CHARS]
        generated = generated_paths(diff)  # the whole PR's, every round
        used = (len(design) + len(claude_md) + len(shown) + len(history) + len(since or "") + len(description)
                + sum(len(g) + 1 for g in generated))
        whole, omitted = full_files(paths, budget=MAX_INPUT_CHARS - used)
        system, user = request(claude_md, design, pr["title"], description, whole, shown, later,
                               omitted, history, generated)
        try:
            found, usage = ask_claude(system, user)
        except CutOff as e:
            # No fingerprint: this diff was not reviewed, so a re-run or the next push reviews it
            github(f"repos/{repo}/pulls/{number}/reviews", token, method="POST", body={
                "event": "COMMENT", "commit_id": pr["head"]["sha"],
                "body": f"**AI review (advisory)**\n\nThe review was cut off ({e}) before it finished. "
                        "Re-run the `ai-review` job, or push again." + usage_marker(e.usage)})
            print(f"Cut off ({e}); posted a note and recorded no fingerprint.")
            return 1
        review = to_github(found, files, left_out, earlier_comments(repo, number, token),
                           round_no=len(done) + 1)
    # The model's text can be steered by the diff: it must not carry a marker that skips a later
    # push or forges the cost. Ours go last, so they are the ones read.
    markers = usage_marker(usage) + (f"\n\n<!-- ai-review-diff: {fp} -->" if fp else "")
    review["body"] = strip_markers(review["body"])
    post = {**review, "body": review["body"] + markers, "commit_id": pr["head"]["sha"]}
    try:
        github(f"repos/{repo}/pulls/{number}/reviews", token, method="POST", body=post)
    except urllib.error.HTTPError as e:
        if e.code != 422 or not review["comments"]:
            raise
        # GitHub refuses the whole review if one inline comment can't be placed: post them in the body
        body = review["body"] + "\n"
        for c in review["comments"]:  # under GitHub's limit, like the body itself
            text = strip_markers(c["body"].replace(MARKER, "").replace(OUTCOME_ASK, "")).strip()
            line = f"\n- `{c['path']}:{c['line']}`: {text}"
            if len(body) + len(line) > MAX_BODY_CHARS + 5_000:
                body += "\n\nMore findings left out: the review body has a size limit."
                break
            body += line
        github(f"repos/{repo}/pulls/{number}/reviews", token, method="POST",
               body={**post, "comments": [], "body": body + markers})
    print(f"Posted a review with {len(review['comments'])} thread(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

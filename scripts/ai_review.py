#!/usr/bin/env python3
"""AI review of a PR (design 6.4: advisory; comments only, never approves).

Round one is the full review: the strongest model, more effort, no cap on findings, with the PR,
the repo's CLAUDE.md, the design sections that apply to the repo and every changed file in full.
Each finding carries an absolute severity (critical, major, minor, nit) and a confidence; critical
and major findings become review threads, minor ones and nits lines in the summary. Later rounds
review only the changes since the last review, with the earlier findings and the replies to them;
in `handbook`, a design changed on `main` since is one of those changes (design 6.4).
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
       scripts/ai_review.py --prompt <repo> <base ref> <design.md> [<earlier findings file>]
                            [--since <commit>]
(in a checkout of the branch: prints the request for a local review or a replay: round one against
<base ref>, the earlier findings, if given, as an earlier round; with --since (the last local run's
commit), a later round on the branch's own files changed since it (in handbook, and a design changed
on main since, with all the branch's files in full to check against it), with the earlier findings and
their outcomes, built as CI's later rounds are, falling back to round one where CI would, or printing
{"nothing": why} when none of the branch's own files changed since. review-branch.py checks
PROMPT_SINCE.)

A PR's description may hold a local-review section, between LOCAL_REVIEW_START and LOCAL_REVIEW_END,
with the local runs' findings and the author's outcomes and reasons (review-branch.py writes it,
design 6.4). The review never reads it, so no review reads the author's reasons first.

On the Peras app's PRs only (design 6.4 "Peras's PRs", 7.13 Stage 10; .github#178), whose drafts
the workflow reviews too, the review leaves out the hidden test files and the builder's disputes:
the files its template lists under PERAS_HIDDEN, and any file the PR adds that is named as a hidden
test, are taken out of the diff, the later rounds' changes and the files read in full; the
PERAS_HIDDEN and PERAS_DISPUTES sections are taken out of the description. A person reviews them.

The review posts as its own GitHub App (REVIEW_APP), which nothing else can post as, so Peras knows
its threads by author (design 7.12); until the app's key is set up, the workflow posts with its own
token as before (LEGACY_REVIEWER), and either one's earlier reviews count as this script's.
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
REVIEW_APP = "akenon-studio-ai-review[bot]"  # who posts the reviews: the review's own GitHub App
LEGACY_REVIEWER = "github-actions[bot]"  # the workflow's own token, before the app (and without its key)
REVIEWER = REVIEW_APP
REVIEWERS = (REVIEW_APP, LEGACY_REVIEWER)
# The Peras app's PRs (design 7.13 Stage 10): their template's fixed sections, by heading
# (peras core/delivery/pr.ts SECTIONS), and the names only its test author gives a hidden test file
# (peras core/builder/policy.ts)
PERAS_APP = "akenon-studio-peras[bot]"
PERAS_HIDDEN, PERAS_DISPUTES = "## Hidden test files", "## Disputes"
HIDDEN_TEST_NAME = re.compile(r"^[^/]+\.hidden\.(?:test|spec)\.[A-Za-z0-9]+$|^test_hidden_[A-Za-z0-9_]+\.py$")
WORD_JOINER = "\u2060"  # Peras defuses @ and # in what it posts with it (design 7.12)
DIFF_MARKER = re.compile(r"<!-- ai-review-diff: ([0-9a-f]{40}) -->")  # in each review's body
# The tokens a review used, in its body, for the monthly scorecard's cost per PR (scripts/ai_scorecard.py)
USAGE_MARKER = re.compile(r"<!-- ai-review-usage: (\{[^<>]*\}) -->")
# On every thread: the reply the scorecard reads, the outcome and the severity after re-grading
# (design 6.4; scripts/ai_scorecard.py reads both)
OUTCOME_ASK = ("Reply with the outcome first, then the severity after re-grading and whether that kept, "
               "downgraded or raised it, in brackets: **Fixed (major, kept)**, **Not an issue (minor, "
               "downgraded)** and why, or **Moved to** the issue's link **(minor, downgraded)**; then resolve "
               "the thread.")
# Every ask a thread may carry, the earlier wordings included, so none reaches the model or a body
OUTCOME_ASKS = (OUTCOME_ASK, "Reply with the outcome first: **Fixed**, **Not an issue** (and why), or "
                "**Moved to** the issue's link; then resolve the thread.")
MAX_DIFF_CHARS = 200_000
# Everything the review reads (design, CLAUDE.md, diff, earlier rounds, files in full): about 120k
# tokens even for code at 3 characters a token, so with MAX_TOKENS of output it stays inside the
# model's 200k context window. The changed files in full get what is left; those that don't fit
# are left out, largest first, and the model is told which.
MAX_INPUT_CHARS = 360_000
MAX_EARLIER_CHARS = 50_000  # the earlier rounds' findings and replies, latest kept
MAX_BODY_CHARS = 60_000  # GitHub refuses a review body over 65,536 characters
MAX_DESCRIPTION_CHARS = 20_000
# The local runs' findings and outcomes in a PR's description, which the review skips (design 6.4)
LOCAL_REVIEW_START = "<!-- local-review:start -->"
LOCAL_REVIEW_END = "<!-- local-review:end -->"
PROMPT_SINCE = True  # --prompt takes --since (review-branch.py checks for it)


def without_ask(text):
    """A thread comment's text without the outcome ask, in any of its wordings."""
    for ask in OUTCOME_ASKS:
        text = text.replace(ask, "")
    return text


def without_local_review(description):
    """The PR's description without its local-review sections; one with no end runs to the end of
    the description, so nothing of it can be read."""
    out, rest = [], description or ""
    while LOCAL_REVIEW_START in rest:
        before, _, after = rest.partition(LOCAL_REVIEW_START)
        out.append(before)
        rest = after.partition(LOCAL_REVIEW_END)[2] if LOCAL_REVIEW_END in after else ""
    out.append(rest)
    return "".join(out)
MAX_GENERATED = 200  # generated paths named in the request; the rest are counted (a vendored tree)
SECTION = re.compile(r"\d+(\.\d+)*")  # a design section number, the only design text posted
# Generated, vendored or lock files: reviewing them costs tokens and finds nothing.
SKIP_PATH = re.compile(r"(^|/)(node_modules|dist|out|build|\.witness)/|\.min\.js$|(^|/)pnpm-lock\.yaml$"
                       r"|^animations/[^/]+/index\.js$|\.snap$")
# The design sections each repo's review reads (design 6.4): structure, architecture, standards;
# Peras is also governed by 7.
DESIGN_SECTIONS = {"peras": ("4", "5", "6", "7")}
DEFAULT_SECTIONS = ("4", "5", "6")
DESIGN_REPO, DESIGN_PATH = "handbook", "design.md"  # the design's own repo: its review reads all of it
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

# For the design's own repo (handbook), "Look for" is about documents, and the design is all of it
DOCS_LOOK_FOR = """Look for:
1. Contradictions: text that disagrees with another part of the design, unchanged parts included
   (an end date, a number, a name, who decides), or with a decision record.
2. One fact in two places (design 6.5): a fact restated instead of pointed to, so the copies can
   drift; and a pointer to a section, file or issue that doesn't exist or says something else.
3. History: a decision that replaces an earlier one without the earlier position in Appendix A, or
   history written into the body, which holds only current decisions; a decision with no date.
4. Rules that can't be followed or checked: a rule too vague to enforce or test, a case it doesn't
   cover that will plausibly happen, a step that depends on something not yet built without saying
   so, or a rule the tools it names can't enforce.
5. Clarity: a term used before it is explained, or a sentence a reader could take two ways.
6. Code: in a workflow or script the PR changes, bugs and security (permissions, secrets,
   untrusted input), as in any code repo.

Leave out what the deterministic checks already enforce: formatting, PR titles, secret scanning,
links to design sections that don't exist. No praise, no style preferences.

The design you get is design.md as this PR leaves it, in full (unless it says it could not be
read): judge each change against the rest of it."""

DOCS_SEVERITY = """For documents, these meanings replace the code examples in the scale above:
- critical: a change that would make work unsafe if followed: a security, privacy or credential
  rule weakened or contradicted, or an approval or check removed by accident.
- major: a contradiction or gap a person or agent will plausibly act on wrongly.
- minor: real but unlikely to mislead.
- nit: polish."""


def instructions(repo):
    """The review's instructions: for the design's own repo, about documents instead of code."""
    if repo != DESIGN_REPO:
        return INSTRUCTIONS
    head, rest = INSTRUCTIONS.split("Look for:", 1)
    scale, finding = rest.split("Severity is absolute.", 1)[1].split("\n\nEach finding:", 1)
    return (head + DOCS_LOOK_FOR + "\n\nSeverity is absolute." + scale + "\n\n" + DOCS_SEVERITY +
            "\n\nEach finding:" + finding)

LATER_ROUND = """

This is a later round. You did the full review earlier; your findings and the replies to them are
below, then the changes pushed since (they can include changes brought in from `main`). Review
only those changes: report a problem they bring, or an earlier finding whose fix is wrong or
incomplete. Don't repeat earlier findings, and don't raise findings about code the earlier review
saw unchanged. Use the same scale; raise an earlier finding's severity only if the new code made
it worse."""

DESIGN_MOVED_NOTE = """The design changed on `main` since the last review; what changed is in
<design_changed_on_main>. It was reviewed in its own PR: don't review it. Check this PR's files,
sent in full, against it: report text or code of this PR, changed since the last review or not, that
the new design now contradicts or makes wrong, on this PR's own lines. Where its lines also show in
<changes_since>, they are `main`'s, not this PR's."""

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


def by_peras(pr):
    """True for a PR the Peras app opened (a bot's login can't be taken by a person)."""
    user = pr.get("user") or {}
    return user.get("login") == PERAS_APP and user.get("type") == "Bot"


def without_section(text, heading):
    """(the text without the section under `heading`, up to the next `## ` heading, the section's
    lines). Every copy of the heading is taken out, so a second one can't hide anything."""
    kept, section, inside = [], [], False
    for line in (text or "").splitlines(keepends=True):
        if line.rstrip() == heading:
            inside = True
            continue
        if inside and line.startswith("## "):
            inside = False
        (section if inside else kept).append(line)
    return "".join(kept), section


def listed_paths(lines):
    """The paths a Peras template section lists, one `- `path`` item a line (its inline code span,
    with Peras's defusing undone)."""
    out = set()
    for line in lines:
        item = line.strip()
        if not item.startswith("- "):
            continue
        item = item[2:].strip()
        fence = len(item) - len(item.lstrip("`"))
        if fence and item.endswith("`" * fence):
            item = item[fence:len(item) - fence].strip()
        item = item.replace(WORD_JOINER, "")
        if item:
            out.add(item)
    return out


def peras_private(description, diff):
    """(the description without the Hidden test files and Disputes sections, the paths to leave
    out: those listed under Hidden test files, and any the diff adds named as a hidden test)."""
    description, hidden = without_section(description, PERAS_HIDDEN)
    description, _ = without_section(description, PERAS_DISPUTES)
    return description, listed_paths(hidden) | added_hidden_tests(diff)


def added_hidden_tests(diff):
    """Paths the diff adds that are named as a hidden test; one already in the repo (a delivered
    regression test, which a builder may change) is reviewed as any file."""
    out, path = set(), None
    for line in (diff or "").splitlines():
        if line.startswith("diff --git "):
            path = line.rstrip().split(" b/", 1)[-1]
        elif line.startswith("new file mode") and path and HIDDEN_TEST_NAME.match(path.rsplit("/", 1)[-1]):
            out.add(path)
    return out


def without_paths(diff, paths):
    """A unified diff without the files at `paths` (by their new path or their old one)."""
    if not paths or diff is None:
        return diff
    out, keep = [], True
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            old, _, new = line.rstrip()[len("diff --git a/"):].partition(" b/")
            keep = old not in paths and new not in paths
        if keep:
            out.append(line)
    return "".join(out)


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
    """The top-level sections of design.md that govern `repo` (DESIGN_SECTIONS), as one text; for
    the design's own repo, all of it."""
    if repo == DESIGN_REPO:
        return design
    want = DESIGN_SECTIONS.get(repo, DEFAULT_SECTIONS)
    parts = re.split(r"(?m)^(?=## )", design)
    return "".join(p for p in parts if re.match(r"## (\d+)\.", p) and
                   re.match(r"## (\d+)\.", p).group(1) in want)


def full_files(paths, root=".", budget=MAX_INPUT_CHARS, keep=()):
    """({path: text} of the changed files as the PR leaves them, [paths left out for size]):
    largest left out first past `budget` characters, those not in `keep` (a later round's touched
    files) before any in it; deleted and binary files are skipped."""
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
        largest = max(found, key=lambda k: (k not in keep, len(found[k])))
        del found[largest]
        omitted.append(largest)
    return found, omitted


NO_DESIGN = "(The design could not be read for this run: judge design fit from CLAUDE.md.)"


def read_design(path, repo, root="."):
    """The design sections for `repo`, or NO_DESIGN when the file is missing or has none. For the
    design's own repo, design.md as the checked-out PR leaves it, so a PR is judged against its own
    version; it is then not sent again as a changed file. Only if the PR deletes or empties it is
    `path` read, which the handbook caller doesn't fetch (it passes no key)."""
    if repo == DESIGN_REPO:
        own, top = os.path.join(root, DESIGN_PATH), os.path.realpath(root)
        # A symlink in the PR could point at a runner file: only a real file in the repo
        if not os.path.islink(own) and os.path.realpath(own).startswith(top + os.sep):
            try:
                with open(own, encoding="utf-8") as f:
                    text = f.read()
                if text.strip():  # an emptied design.md: `path` below (none from the handbook caller)
                    return text
            except (OSError, UnicodeDecodeError):
                pass
    try:
        with open(path) as f:
            return design_sections(f.read(), repo) or NO_DESIGN
    except (TypeError, OSError):  # TypeError: no path given
        return NO_DESIGN


def review_paths(paths, repo):
    """The changed files to send in full: for the design's own repo, not design.md, which is the
    design already (read_design)."""
    return [p for p in paths if not (repo == DESIGN_REPO and p == DESIGN_PATH)]


def tagged(tag, text):
    return f"<{tag}>\n{text}\n</{tag}>"


def request(claude_md, design, title, description, files, diff_text, later=None, omitted=(),
            history="", generated=(), repo="", design_changes=""):
    """(system, user) for the Messages API. The system holds what is the same for every round and
    PR of a repo (instructions, CLAUDE.md, design), so it is cached; `later` is (earlier findings
    and replies, changes since the last review) for a later round, which says so in the user
    message to keep the cached prefix the same. A later round's `files` are those the changes
    touch (all the PR's, when the design moved on `main`: later_paths), and it gets no `diff_text`. `history` is the earlier rounds for a full review that isn't
    the first (CLAUDE.md changed, or too much changed), so it doesn't repeat what was settled. For
    the design's own repo the design is the PR's, which most pushes change, so it isn't cached:
    writing it to the cache costs more than reading it plainly. `design_changes` is, in a later
    round there, the design's diff brought in from `main` (design_moved), labelled as such."""
    shared = {"type": "text", "text": tagged("claude_md", claude_md) + "\n\n" + tagged("design", design)}
    if repo != DESIGN_REPO:
        shared["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
    system = [{"type": "text", "text": instructions(repo)}, shared]
    user = ((LATER_ROUND.strip() + "\n\n" if later else "") + f"<pr_title>{title}</pr_title>\n" + tagged("pr_description", description) + "\n\n" +
            "\n".join(f'<file path="{p}">\n{t}\n</file>' for p, t in files.items()) + "\n\n" +
            (tagged("files_omitted", "\n".join(omitted)) + "\n\n" if omitted else "") +
            (tagged("generated_files", "\n".join(generated)) + "\n\n" if generated else "") +
            (tagged("diff", diff_text) if diff_text else ""))
    if later:
        user += "\n\n" + tagged("earlier_review", later[0]) + "\n\n" + tagged("changes_since", later[1])
        if design_changes:
            user += "\n\n" + DESIGN_MOVED_NOTE + "\n" + tagged("design_changed_on_main", design_changes)
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
    return [r for r in reviews if (r.get("user") or {}).get("login") in REVIEWERS
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


def git_out(*args, strip=True):
    """A git command's output, stripped unless told not to; None when it fails."""
    try:
        run = subprocess.run(["git", *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return (run.stdout.strip() if strip else run.stdout) if run.returncode == 0 else None


def later_changes(commit, repo, paths, base=None):
    """(changes since, design moved, size) for a later round from an earlier reviewed commit: the
    PR's files' diff since it (changes_since; None for a full round), in the design's own repo the
    design's diff brought in from `main` since (design_moved), labelled apart in the request, and
    how much changed, for the "too much changed" limit: main's design diff counted once, also when
    a conflict leaves it in the changes since too."""
    since = changes_since(commit, list(paths))
    if since is None:
        return None, "", 0
    moved, own = design_moved(commit, repo, paths, base)
    if own is not None:  # the PR's own design changes since, without main's
        others = [p for p in paths if p != DESIGN_PATH]  # no paths would diff the whole tree
        since = ((changes_since(commit, others) or "") if others else "") + own
    mixed = DESIGN_PATH in paths and own is None  # main's design diff is in `since` already
    return since, moved, len(since) + (0 if mixed else len(moved))


def design_moved(commit, repo, paths, base=None):
    """(main's design diff, the PR's own design diff) since an earlier reviewed commit, in the
    design's own repo: a later round is on what changed in the design, since the PR is judged
    against the rest of it (6.4). For a PR that doesn't change the design, main's diff is the
    design's diff since (main brought it in), and the own diff None. For one that does, main's is
    the design's diff between where the branch met `base` at that commit and where it meets it now,
    and the own diff is HEAD against that commit with main's diff applied; None (the PR's design
    diff since then shows both, main's labelled apart) when the two conflict in the design.
    ("", None) outside the design's repo, when main's design didn't move, the commit isn't in the
    history, or there is no `base` (nor its merge base) to tell main's part by: the PR's design diff
    since shows both then, unlabelled, as before .github#195."""
    if repo != DESIGN_REPO:
        return "", None
    if DESIGN_PATH not in paths:
        return changes_since(commit, [DESIGN_PATH]) or "", None
    before = git_out("merge-base", commit, base) if base else None
    now = git_out("merge-base", "HEAD", base) if base else None
    if not before or not now:
        print(f"No merge base with {base!r}: main's design changes, if any, aren't labelled apart.", file=sys.stderr)
        return "", None
    if before == now:
        return "", None
    moved = git_out("diff", before, now, "--", DESIGN_PATH, strip=False)  # a hunk can end in a blank line
    if not moved:
        return "", None
    # the reviewed commit with main's changes since brought in; what HEAD adds to it is the PR's own.
    # A conflict elsewhere (exit 1, the conflicted paths after the tree) leaves the design's merge good
    try:
        run = subprocess.run(["git", "merge-tree", "--write-tree", "--name-only", commit, now],
                             capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        run = None
    if not run or run.returncode not in (0, 1):  # git older than 2.38 has no --write-tree
        print("git merge-tree failed (" + (run.stderr.strip() if run else "didn't run") + "): the round shows "
              "the design's own changes and main's together, main's labelled.", file=sys.stderr)
        return moved, None
    lines = run.stdout.split("\n")
    conflicted = lines[1:lines.index("", 1)] if "" in lines[1:] else lines[1:]
    if not lines[0] or DESIGN_PATH in conflicted:
        print("The design's own changes and main's conflict: the round shows both, main's labelled.", file=sys.stderr)
        return moved, None
    return moved, changes_since(lines[0], [DESIGN_PATH])


def later_paths(changed, files, moved):
    """The files a later round sends in full: those its changes touch, and when the design moved on
    `main` (design_moved), all the PR's own, which are what it checks against the change."""
    return list(dict.fromkeys(list(changed) + (list(files) if moved else [])))


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
            lines.append(f"[{who}] {without_ask(c['body'].replace(MARKER, '')).strip()}")
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


def prompt_main(repo, base, design_path, settled_path=None, since=None):
    """Print the request for the checked-out branch against `base` (local review, replay): round one,
    or with `since` (the last local run's commit) a later round on the branch's own files changed
    since it, built as CI's later rounds are, with round one instead where CI would have one (the
    commit no longer in the history, CLAUDE.md changed since, or too much changed), and
    `{"nothing": why}` printed instead of a request when none of the branch's own files changed
    since, as CI's fingerprint skips such a push. `settled_path`:
    the earlier findings, their outcomes and the partners' answers (review-branch.py, design 6.4),
    as an earlier round."""
    diff = subprocess.run(["git", "diff", f"{base}...HEAD"], capture_output=True, text=True, check=True).stdout
    files = split_diff(diff)
    text, _ = review_input(files)
    claude_md = open("CLAUDE.md").read() if os.path.exists("CLAUDE.md") else ""
    title = subprocess.run(["git", "log", "-1", "--format=%s"], capture_output=True, text=True).stdout.strip()
    design = read_design(design_path, repo)
    if design == NO_DESIGN:
        raise SystemExit(f"can't read the design for {repo} from {design_path}")
    history = ""
    if settled_path:
        try:
            with open(settled_path) as f:
                history = f.read()[-MAX_EARLIER_CHARS:]  # latest kept, as earlier_review does
        except OSError as e:
            raise SystemExit(f"can't read the settled findings from {settled_path}: {e}")
    changes, moved = None, ""
    if since and files and subprocess.run(["git", "merge-base", "--is-ancestor", since, "HEAD"],
                                capture_output=True).returncode == 0:
        # the branch's own files only, and in handbook a design changed on main apart, as in CI
        changes, moved, size = later_changes(since, repo, files, base)
        if changes is not None and not changes.strip() and not moved and not changes_since(since, ["CLAUDE.md"]):
            # nothing of the branch's own changed since (main merged in, a lockfile): nothing to review,
            # as CI's fingerprint skips such a push
            print(json.dumps({"nothing": "nothing of the branch's own changed since the last run"}))
            return 0
        # round one when CLAUDE.md changed since, or too much changed
        if changes is not None and (changes_since(since, ["CLAUDE.md"]) or size > MAX_DIFF_CHARS):
            changes = None
    if changes is not None:  # a later round: the changes, and the files they touch in full
        changed = split_diff(changes)  # generated files left out, as in CI
        shown = "".join(changed.values())
        generated = generated_paths(diff)
        budget = (MAX_INPUT_CHARS - len(shown) - len(moved) - len(design) - len(history) - len(claude_md)
                  - sum(len(g) + 1 for g in generated))  # as CI counts it
        whole, omitted = full_files(review_paths(later_paths(changed, files, moved), repo), budget=budget,
                                    keep=set(changed))
        system, user = request(claude_md, design, title, "", whole, "", later=(history, shown), omitted=omitted,
                               generated=generated, repo=repo, design_changes=moved)  # the whole branch's, as in CI
    else:
        generated = generated_paths(diff)
        budget = (MAX_INPUT_CHARS - len(text) - len(design) - len(history) - len(claude_md)
                  - sum(len(g) + 1 for g in generated))
        whole, omitted = full_files(review_paths(files, repo), budget=budget)
        system, user = request(claude_md, design, title, "", whole, text, omitted=omitted,
                               generated=generated, repo=repo, history=history)
    print(json.dumps({"system": system, "user": user}))
    return 0


def main(argv):
    if argv[:1] == ["--prompt"]:
        rest = list(argv[1:])
        since = None
        if "--since" in rest:
            at = rest.index("--since")
            since = rest[at + 1] if at + 1 < len(rest) else None
            del rest[at:at + 2]
        return prompt_main(*rest[:4], since=since)
    token, repo, number = (os.environ["GITHUB_TOKEN"], os.environ["GITHUB_REPOSITORY"],
                           os.environ["PR_NUMBER"])
    pr = github(f"repos/{repo}/pulls/{number}", token)
    try:
        diff = github(f"repos/{repo}/pulls/{number}", token, accept="application/vnd.github.diff")
    except urllib.error.HTTPError as e:
        if e.code != 406:
            raise
        diff = None  # GitHub refuses diffs over its size limits
    description = without_local_review(pr.get("body") or "")
    hidden = set()
    if by_peras(pr):  # its hidden tests and the builder's disputes are a person's to read
        description, hidden = peras_private(description, diff)
        diff = without_paths(diff, hidden)
    files = split_diff(diff) if diff is not None else {}
    if diff is not None and not files:
        print("Nothing to review: the PR changes only generated or lock files.")
        return 0
    try:
        claude_md = open("CLAUDE.md").read()
    except OSError:
        claude_md = ""
    reviews = all_reviews(repo, number, token)
    # For the design's own repo, a change elsewhere in the design (main brought in) is a new review:
    # what it is judged against changed, as with CLAUDE.md
    name = repo.split("/")[1]
    against = claude_md + (read_design(os.environ.get("DESIGN_FILE"), name) if name == DESIGN_REPO else "")
    fp = fingerprint(files, against) if files else None
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
        # in handbook, a design changed on main is what the round is for, labelled apart from the PR's
        base = (pr.get("base") or {}).get("ref")
        since, moved, size = (later_changes(done[-1]["commit_id"], name, files, base and f"origin/{base}")
                              if done else (None, "", 0))
        since = without_paths(since, hidden)
        # A full round instead when a new CLAUDE.md rule applies to the whole PR, or the changes
        # are as big as a diff can be
        if since is not None and (changes_since(done[-1]["commit_id"], ["CLAUDE.md"])
                                  or size > MAX_DIFF_CHARS):
            since, moved = None, ""
        history = earlier_review(repo, number, token, done) if done else ""
        if since is not None:  # a later round: the changes, and the files they touch in full
            later, shown, paths = (history, since), "", later_paths(split_diff(since), files, moved)
        else:
            later, shown, paths = None, text, list(files)
        description = description[:MAX_DESCRIPTION_CHARS]
        generated = generated_paths(diff)  # the whole PR's, every round
        used = (len(design) + len(claude_md) + len(shown) + len(history) + len(since or "") + len(moved) + len(description)
                + sum(len(g) + 1 for g in generated))
        whole, omitted = full_files(review_paths(paths, repo.split("/")[1]), budget=MAX_INPUT_CHARS - used,
                                    keep=set(split_diff(since)) if since is not None else ())
        system, user = request(claude_md, design, pr["title"], description, whole, shown, later,
                               omitted, history, generated, repo.split("/")[1], design_changes=moved)
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
            text = strip_markers(without_ask(c["body"].replace(MARKER, ""))).strip()
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

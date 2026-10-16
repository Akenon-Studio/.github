"""Tests for scripts/ai_scorecard.py. Run: python3 -m unittest discover tests"""

import datetime
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from ai_scorecard import (FIX_TITLE, body, is_fix, local_runs, commented_lines, cost, escapes, is_round, last_month,  # noqa: E402
                          local_findings, month_range, outcome, pr_record, regraded, removed_lines,
                          score, severity, shares, thread_outcome, usage_of, was_there)

MARKER = "<!-- ai-review -->"
USAGE = '<!-- ai-review-usage: {"model": "claude-opus-5-5", "input": 1000000, "cache_read": 0, ' \
        '"cache_write": 0, "output": 100000} -->'


def review(id, at, commit, text="Summary.", login="github-actions", header="**AI review (advisory), round 1**"):
    return {"id": id, "author": {"login": login}, "submittedAt": at, "commit": {"oid": commit},
            "body": f"{header}\n\n{text}\n\n{USAGE}"}


def thread(review_id, head, line, replies=(), resolved=True, path="a.py", by="RuvinduH"):
    comments = [{"author": {"login": "github-actions"}, "body": f"{head}\n\nx\n\n{MARKER}",
                 "url": f"u/{review_id}/{line}", "diffHunk": f"@@ -1 +1 @@\n+{line}",
                 "pullRequestReview": {"id": review_id}}]
    comments += [{"author": {"login": by}, "body": r, "url": "", "diffHunk": "",
                  "pullRequestReview": None} for r in replies]
    return {"isResolved": resolved, "path": path, "comments": {"nodes": comments}}


class PartsTest(unittest.TestCase):
    def test_months(self):
        self.assertEqual(last_month(datetime.date(2026, 11, 1)), "2026-10")
        self.assertEqual(last_month(datetime.date(2027, 1, 15)), "2026-12")
        self.assertEqual(month_range("2026-02"), "2026-02-01..2026-02-28")

    def test_severity_from_the_heading(self):
        self.assertEqual(severity("**Major** · high confidence · design 6.2\n\ntext"), "major")
        self.assertEqual(severity("text from the old reviewer"), "unrated")

    def test_the_first_reply_that_states_an_outcome(self):
        self.assertEqual(outcome(["Thanks", "**Fixed** in abc"]), "fixed")
        self.assertEqual(outcome(["Not an issue: the caller checks it"]), "not an issue")
        self.assertEqual(outcome(["Moved to #12"]), "moved")
        self.assertIsNone(outcome(["Done, I think"]))

    def test_the_commented_lines_end_the_hunk(self):
        hunk = "@@ -1,4 +1,5 @@\n a = 1\n-gone()\n+\n b = 2\n+  return total(x)"
        self.assertEqual(commented_lines(hunk), ["a = 1", "b = 2", "return total(x)"])
        self.assertEqual(commented_lines("@@ -1 +0,0 @@\n-removed()"), [])
        self.assertEqual(commented_lines(""), [])

    def test_was_there_means_whole_lines_in_order(self):
        text = "def f():\n    total = add(a, b)\n\n    return total\n"
        self.assertTrue(was_there(["total = add(a, b)", "return total"], text))
        self.assertFalse(was_there(["return total", "total = add(a, b)"], text))  # order
        self.assertFalse(was_there(["total = add(a, b", "return total"], text))  # a part of a line
        self.assertFalse(was_there(["return total"], text))  # too short to tell

    def test_notes_are_not_rounds(self):
        self.assertTrue(is_round("**AI review (advisory), round 2**\n\nThe review was cut off before, now fine."))
        self.assertTrue(is_round("**AI review (advisory)**\n\nOld reviewer summary."))
        self.assertFalse(is_round("**AI review (advisory)**\n\nThe review was cut off (max_tokens) before..."))
        self.assertFalse(is_round("**AI review (advisory)**\n\nNot reviewed: the diff is too large."))

    def test_the_last_usage_marker_and_a_broken_one(self):
        self.assertEqual(usage_of('x <!-- ai-review-usage: {"a": 1} --> <!-- ai-review-usage: {"a": 2} -->'),
                         {"a": 2})
        self.assertIsNone(usage_of("<!-- ai-review-usage: {not json} -->"))
        self.assertIsNone(usage_of("no marker"))

    def test_cost_at_the_price_table(self):
        self.assertAlmostEqual(cost({"model": "claude-opus-5-5", "input": 1_000_000, "output": 100_000,
                                     "cache_read": 1_000_000, "cache_write": 0}), 4 + 2 + 0.2)
        self.assertIsNone(cost({"model": "unknown"}))
        # a dated snapshot id, or priced on the model that was asked for
        self.assertAlmostEqual(cost({"model": "claude-opus-5-5-20261001", "output": 1_000_000}), 20)
        self.assertAlmostEqual(cost({"model": "other", "requested": "claude-opus-5-5", "input": 1_000_000}), 4)


class RecordTest(unittest.TestCase):
    def data(self):
        old = "x = compute_everything(inputs)"
        return {"reviews": {"nodes": [
            review("r2", "2026-10-11T02:00:00Z", "c2"), review("r1", "2026-10-11T01:00:00Z", "c1"),
            review("note", "2026-10-11T03:00:00Z", "c3", "The review was cut off (max_tokens)",
                   header="**AI review (advisory)**"),
            review("x", "2026-10-11T00:00:00Z", "c0", login="someone")]},
            "reviewThreads": {"nodes": [
                thread("r1", "**Major** · high confidence", "y = new_code_here()", ["Fixed in c2"]),
                thread("r2", "**Critical** · high confidence", old, ["Not an issue: guarded upstream"]),
                thread("r2", "**Major** · high confidence", "z = added_in_round_two()", []),
                thread("r2", "**Minor** · high confidence", old, ["ok"]),
                thread("r1", "old reviewer text", "w = something_old()", ["looks fine"])]}}

    def test_rounds_late_catches_and_outcomes(self):
        reads = []

        def read(repo, sha, path):
            reads.append((repo, sha, path))
            return "x = compute_everything(inputs)\n"
        rec = pr_record("platform", 7, "u", self.data(), read)
        self.assertEqual(rec["rounds"], 2)  # the cut-off note and someone else's review aren't rounds
        self.assertEqual(reads, [("platform", "c1", "a.py")])  # round one's commit, read once
        self.assertAlmostEqual(rec["cost"], 3 * 6.0)  # every review cost tokens, the note too
        t = rec["threads"]
        self.assertEqual([x["round"] for x in t], [1, 2, 2, 2, 1])
        self.assertEqual([x["late"] for x in t], [False, True, False, False, False])
        self.assertEqual([x["outcome"] for x in t], ["fixed", "not an issue", None, None, None])

    def test_only_the_reviews_threads_and_members_replies_count(self):
        # design 6.2: an outsider's reply, or a thread an outsider opened with the marker, is ignored
        data = self.data()
        data["reviewThreads"]["nodes"] = [
            thread("r1", "**Major** · high confidence", "y = new_code_here()", ["Fixed"], by="someone"),
            dict(thread("r1", "**Major** · high confidence", "q = faked()", ["Fixed"]))]
        data["reviewThreads"]["nodes"][1]["comments"]["nodes"][0]["author"] = {"login": "someone"}
        rec = pr_record("platform", 7, "u", data, lambda *a: "")
        self.assertEqual([t["outcome"] for t in rec["threads"]], [None])

    def test_the_orgs_apps_replies_count(self):
        # design 6.2, 6.4: Peras answering as an app counts; an outsider and the reviewer don't
        data = self.data()
        t = thread("r1", "**Major** · high confidence", "y = new_code_here()", [])
        t["comments"]["nodes"] += [
            {"author": {"__typename": "User", "login": "someone"}, "body": "Not an issue"},
            {"author": {"__typename": "Bot", "login": "github-actions"}, "body": "Not an issue"},
            {"author": {"__typename": "Bot", "login": "akenon-studio-peras"}, "body": "Fixed in c2"}]
        data["reviewThreads"]["nodes"] = [t]
        rec = pr_record("platform", 7, "u", data, lambda *a: "")
        self.assertEqual([x["outcome"] for x in rec["threads"]], ["fixed"])

    def test_cost_is_over_every_pr_the_ai_review_ran_on(self):
        # a PR whose only AI review is a cut-off note costs tokens but has no round
        note = {"reviews": {"nodes": [review("n", "2026-10-11T01:00:00Z", "c1", "The review was cut off (max_tokens)",
                                             header="**AI review (advisory)**")]}, "reviewThreads": {"nodes": []}}
        rec = pr_record("platform", 3, "u", note, lambda *a: "")
        s = score([rec, pr_record("platform", 7, "u", self.data(), lambda *a: "")])
        self.assertEqual((s["costed"], s["ci_prs"]), (2, 2))

    def test_a_pr_the_review_never_ran_on(self):
        self.assertIsNone(pr_record("platform", 8, "u", {"reviews": {"nodes": []},
                                                         "reviewThreads": {"nodes": []}}))

    def test_the_score_and_report(self):
        rec = pr_record("platform", 7, "u", self.data(), lambda *a: "x = compute_everything(inputs)")
        s = score([rec])
        self.assertEqual(len(s["late"]), 1)
        self.assertEqual(len(s["false_alarms"]), 1)
        self.assertEqual(s["serious"], 3)
        self.assertEqual(s["unrated"], 1)
        self.assertEqual(len(s["unreadable"]), 2)  # resolved with no reply, and with "ok"
        text = body("2026-10", s)
        self.assertIn("| 1 of 3 |", text)
        self.assertIn("1 thread(s) from the reviewer before 2026-10-10", text)

    def test_open_findings_are_never_named(self):
        # design 6.2: a resolved critical thread with no outcome, or a late catch still open, is
        # counted in the public issue, never named
        data = self.data()
        data["reviewThreads"]["nodes"] = [
            thread("r2", "**Critical** · high confidence", "x = compute_everything(inputs)", ["done"]),
            thread("r2", "**Major** · high confidence", "x = compute_everything(inputs)", ["Fixed in c3"])]
        rec = pr_record("platform", 7, "u", data, lambda *a: "x = compute_everything(inputs)")
        text = body("2026-10", score([rec]))
        self.assertNotIn("critical", text.split("\nLate catches:\n")[1].split("### Why")[0])
        self.assertIn("- [platform#7, round 2, major](u/r2/x = compute_everything(inputs))", text)
        self.assertIn("- 1 still open, counted only", text)
        self.assertIn("No readable outcome (the reply should start with Fixed, Not an issue or Moved to, then the "
                      "severity\nafter re-grading in brackets: `Fixed (major, kept)`): 1,", text)
        self.assertIn("### Done when", text)  # a Task form, so issue-fields can file it

    def test_a_long_pr_is_named(self):
        long = {"repo": "platform", "number": 8, "url": "u8", "rounds": 4, "cost": None, "threads": []}
        self.assertIn("Round 4 or later:\n- [platform#8, 4 rounds](u8)", body("2026-10", score([long])))


# review-branch.py's section as it writes it (peras/core/workspace/bin/review-branch.py, section())
SECTION = """Summary.

<!-- local-review:start -->
## Local review
2 local run(s) (design 6.4); each critical or major finding and what became of it:

| Finding | Where | Claimed | After re-grading | Outcome | Why |
|---|---|---|---|---|---|
| R1.1 | `scripts/a.py:12` | major | | fixed | |
| R1.2 | `scripts/a.py:40` | critical | minor | issue .github#300 (an agent's, settled) | rare \\| edge |
| R1.3 | `b.py:3` | major | minor | not-an-issue (an agent's, not settled) | guarded |
| R2.1 | `b.py:9` | major | | none | |
<!-- local-review:end -->

Closes #1"""


def reply(text, login="RuvinduH", typename="User"):
    return {"author": {"__typename": typename, "login": login}, "body": text}


class LocalSectionTest(unittest.TestCase):
    def test_rows_as_review_branch_writes_them(self):
        found = local_findings(SECTION)
        self.assertEqual([f["id"] for f in found], ["R1.1", "R1.2", "R1.3", "R2.1"])
        self.assertEqual([f["round"] for f in found], [1, 1, 1, 2])
        self.assertEqual([f["regraded"] for f in found], [None, "minor", "minor", None])
        self.assertEqual([f["outcome"] for f in found], ["fixed", "issue", "not an issue", None])
        # an agent's re-grade is a dispute until settled, by the row's mark, never by the PR's author
        self.assertEqual([f["disputed"] for f in found], [False, False, True, False])
        self.assertEqual(found[0]["path"], "scripts/a.py")

    def test_runs_and_peras_rows(self):
        self.assertEqual(local_runs(SECTION), 2)
        self.assertEqual(local_runs("no section"), 0)
        # on a Peras PR, a fix with no severity after re-grading waits for the person's re-grade
        self.assertEqual([f["ungraded"] for f in local_findings(SECTION, peras_pr=True)],
                         [True, False, False, False])

    def test_only_inside_the_markers(self):
        self.assertEqual(local_findings("| R1.1 | `a:1` | major | | fixed | |"), [])
        self.assertEqual(len(local_findings(SECTION.replace("<!-- local-review:end -->", ""))), 4)

    def test_the_section_counts_only_in_the_orgs_own_description(self):
        data = {"reviews": {"nodes": []}, "reviewThreads": {"nodes": []}, "body": SECTION,
                "author": {"__typename": "User", "login": "RuvinduH"}}
        self.assertEqual(len(pr_record("platform", 9, "u", data)["local"]), 4)
        self.assertIsNone(pr_record("platform", 9, "u", dict(data, editor={"login": "someone"})))
        self.assertIsNone(pr_record("platform", 9, "u", dict(data, author={"login": "someone"})))


class ThreadOutcomeTest(unittest.TestCase):
    def test_a_persons_reply_is_their_call(self):
        got = thread_outcome([reply("Not an issue, re-graded minor: caller checks it")], False, "major")
        self.assertEqual((got["outcome"], got["regraded"], got["disputed"]), ("not an issue", "minor", False))

    def test_an_agents_re_grade_is_a_dispute_until_a_person_answers(self):
        agent = reply("Not an issue: unreachable.\n\n<!-- agent -->")
        self.assertTrue(thread_outcome([agent], False, "major")["disputed"])
        settled = thread_outcome([agent, reply("Not an issue, agreed.")], False, "major")
        self.assertEqual((settled["outcome"], settled["disputed"]), ("not an issue", False))

    def test_an_agents_fix_counts(self):
        got = thread_outcome([reply("Fixed in abc.\n\n<!-- agent -->")], False, "major")
        self.assertEqual((got["outcome"], got["disputed"], got["regraded"]), ("fixed", False, None))

    def test_an_agents_own_re_grade_with_a_fix_is_a_dispute(self):
        agent = reply("Fixed in abc, re-graded minor.\n\n<!-- agent -->")
        self.assertTrue(thread_outcome([agent], False, "major")["disputed"])
        got = thread_outcome([agent, reply("Agreed, re-graded minor.")], False, "major")
        self.assertEqual((got["disputed"], got["regraded"]), (False, "minor"))

    def test_a_person_agreeing_keeps_the_agents_re_grade(self):
        agent = reply("Moved to #5, re-graded minor.\n\n<!-- agent -->")
        got = thread_outcome([agent, reply("Moved to #5, agreed")], False, "major")
        self.assertEqual((got["outcome"], got["regraded"], got["disputed"]), ("moved", "minor", False))
        # another outcome than the agent's sets its re-grade aside
        got = thread_outcome([agent, reply("Fixed in abc")], False, "major")
        self.assertEqual((got["outcome"], got["regraded"]), ("fixed", None))
        # a re-grade in a later reply than the outcome still counts
        got = thread_outcome([reply("Fixed"), reply("and re-graded minor")], False, "major")
        self.assertEqual(got["regraded"], "minor")

    def test_peras_fixes_are_ungraded_until_a_person_re_grades(self):
        peras = reply("**Fixed** in abc", "akenon-studio-peras", "Bot")
        self.assertTrue(thread_outcome([peras], True, "major")["ungraded"])
        got = thread_outcome([peras, reply("Re-graded minor: the input can't be empty here.")], True, "major")
        self.assertEqual((got["ungraded"], got["regraded"]), (False, "minor"))
        # Peras's "disputed" alone is no outcome (design 7.13 Stage 10)
        self.assertIsNone(thread_outcome([reply("disputed", "akenon-studio-peras", "Bot")], True, "major")["outcome"])

    def test_the_latest_reply_with_an_outcome_decides(self):
        replies = [reply("Not an issue: unreachable.\n\n<!-- agent -->"), reply("Please fix this."),
                   reply("Fixed in def.\n\n<!-- agent -->")]
        got = thread_outcome(replies, False, "major")
        self.assertEqual((got["outcome"], got["disputed"]), ("fixed", False))
        # a person who stated the agent's outcome before settles the agent's repeat of it
        got = thread_outcome([reply("Not an issue"), reply("Not an issue\n\n<!-- agent -->")], False, "major")
        self.assertEqual((got["outcome"], got["disputed"]), ("not an issue", False))

    def test_a_persons_bare_fixed_leaves_a_peras_fix_ungraded(self):
        peras = reply("**Fixed** in abc", "akenon-studio-peras", "Bot")
        self.assertTrue(thread_outcome([peras, reply("Fixed, thanks")], True, "major")["ungraded"])
        self.assertFalse(thread_outcome([peras, reply("Fixed, re-graded major")], True, "major")["ungraded"])

    def test_the_severity_the_reviewer_asks_for_in_brackets(self):
        # the reply ai_review.py asks for (.github#201): outcome, then the severity after re-grading
        self.assertEqual(regraded("**Fixed (major, kept)** in abc"), "major")
        self.assertEqual(regraded("Not an issue (Minor, downgraded): the caller checks it"), "minor")
        self.assertEqual(regraded("**Moved to** #12 **(minor, downgraded)**"), "minor")
        self.assertEqual(regraded("Fixed (critical)"), "critical")
        self.assertIsNone(regraded("Not an issue (major claim, but unreachable)"))
        self.assertIsNone(regraded("Fixed in abc.\n\nSee (minor) below"))  # first line only
        self.assertIsNone(regraded("Why (minor)? The input is user-controlled."))  # no outcome stated
        self.assertIsNone(regraded("Please fix this (minor)"))
        agent = reply("Fixed (minor, downgraded)\n\n<!-- agent -->")
        got = thread_outcome([agent, reply("Why (minor)? The input is user-controlled.")], False, "major")
        self.assertEqual((got["disputed"], got["regraded"]), (True, "minor"))  # a question settles nothing
        got = thread_outcome([reply("Fixed (major, kept) in abc.\n\n<!-- agent -->")], False, "major")
        self.assertEqual((got["outcome"], got["regraded"], got["disputed"]), ("fixed", "major", False))
        got = thread_outcome([reply("Fixed (minor, downgraded)\n\n<!-- agent -->")], False, "major")
        self.assertTrue(got["disputed"])  # an agent's own downgrade is still a dispute

    def test_the_reply_the_review_asks_for_reads_back(self):
        # the ask's own examples are what the scorecard reads
        import ai_review
        self.assertIn("**Fixed (major, kept)**", ai_review.OUTCOME_ASK)
        self.assertEqual(outcome(["**Fixed (major, kept)**"]), "fixed")
        self.assertEqual(regraded("**Not an issue (minor, downgraded)** and why"), "minor")

    def test_regrade_phrases(self):
        self.assertEqual(regraded("downgraded to **Minor**"), "minor")
        self.assertEqual(regraded("re-graded: nit"), "nit")
        self.assertEqual(regraded("raised to critical"), "critical")
        self.assertIsNone(regraded("this is a minor point"))
        self.assertIsNone(regraded("Not an issue: it raises minor overhead"))
        self.assertIsNone(regraded("Fixed; the point raised major concerns"))
        self.assertIsNone(regraded("downgrading minor details"))


class SharesTest(unittest.TestCase):
    def test_by_round_for_local_and_ci_apart(self):
        data = {"reviews": {"nodes": [review("r1", "2026-10-11T01:00:00Z", "c1")]}, "body": SECTION,
                "author": {"__typename": "User", "login": "RuvinduH"}, "reviewThreads": {"nodes": [
                    thread("r1", "**Major** · high confidence", "y = new_code_here()", ["Fixed, re-graded minor"]),
                    thread("r1", "**Critical** · high", "z = other_code()", ["Moved to #5 (critical, kept)"]),
                    thread("r1", "**Major** · high", "w = more_code()", ["Fixed"]),
                    thread("r1", "**Minor** · high", "q = minor_code()", ["Fixed"])]}}
        rec = pr_record("platform", 7, "u", data, lambda *a: "")
        table = shares([rec])
        self.assertEqual(list(table), [("local", 1), ("local", 2), ("CI", 1)])
        local1 = table["local", 1]
        self.assertEqual((local1["findings"], local1["counted"], local1["disputed"]), (3, 2, 1))
        self.assertEqual((local1["fixed"], local1["issue"], local1["kept"], local1["downgraded"]), (1, 1, 1, 1))
        self.assertEqual(table["local", 2]["no outcome"], 1)
        ci = table["CI", 1]  # the minor thread isn't a critical or major finding
        self.assertEqual((ci["findings"], ci["fixed"], ci["moved"], ci["downgraded"], ci["kept"]), (3, 2, 1, 1, 1))
        # a reply that names no severity is unstated, not kept (.github#201)
        self.assertEqual(ci["unstated"], 1)
        s = score([rec])
        self.assertEqual(len(s["disputed"]), 1)
        self.assertEqual([f["id"] for f in s["no_outcome"]], ["R2.1"])
        self.assertEqual((s["runs_mean"], s["local_prs"]), (2, 1))
        text = body("2026-10", s)
        self.assertIn("| local | 1 | 3 | 2 | 1 (50%) | 1 (50%) | 0 (0%) | 0 (0%) | 1 (50%) | 1 (50%) |", text)
        self.assertIn("| CI | 1 | 3 | 3 | 1 (33%) | 1 (33%) | 0 (0%) | 1 (33%) | 2 (67%) |", text)
        # the issue is public (design 6.2): a finding still open is counted, never named
        self.assertNotIn("R1.3", text)
        self.assertNotIn("Disputed, not yet", text)


class EscapesTest(unittest.TestCase):
    PATCH = "@@ -10,4 +10,4 @@\n keep()\n-old_one()\n-old_two()\n+new()\n+new2()\n keep()\n@@ -30,1 +30,2 @@\n same()\n+added()"

    def test_removed_lines_are_the_old_side(self):
        self.assertEqual(removed_lines(self.PATCH), [11, 12])
        self.assertEqual(removed_lines("@@ -1,0 +1,2 @@\n+a\n+b"), [])

    def test_a_no_issue_bots_fix_is_no_escape(self):
        renovate = {"__typename": "Bot", "login": "renovate"}
        self.assertFalse(is_fix({"title": "fix(deps): update x", "author": renovate}))
        self.assertTrue(is_fix({"title": "fix(deps): update x", "author": {"__typename": "User", "login": "RuvinduH"}}))
        self.assertTrue(is_fix({"title": "fix: x", "author": {"__typename": "Bot", "login": "akenon-studio-peras"}}))
        self.assertFalse(is_fix({"title": "feat: x", "author": {"__typename": "User", "login": "RuvinduH"}}))

    def test_fix_titles(self):
        self.assertTrue(FIX_TITLE.match("fix(ai-review): x"))
        self.assertTrue(FIX_TITLE.match("fix!: x"))
        self.assertFalse(FIX_TITLE.match("feat: fix x"))
        self.assertFalse(FIX_TITLE.match("fixup: x"))

    def test_a_fix_changing_a_reviewed_prs_recent_lines(self):
        def pr(n, merged):
            return {"commit": {"associatedPullRequests": {"nodes": [{"number": n, "url": f"u{n}", "mergedAt": merged}]}}}
        ranges = [dict(pr(4, "2026-09-20T00:00:00Z"), startingLine=1, endingLine=11),  # reviewed, recent
                  dict(pr(3, "2026-05-01T00:00:00Z"), startingLine=12, endingLine=12),  # over 90 days
                  dict(pr(2, "2026-09-25T00:00:00Z"), startingLine=13, endingLine=99)]  # not reviewed
        files = [{"filename": "scripts/a.py", "status": "modified", "patch": self.PATCH},
                 {"filename": "new.py", "status": "added", "patch": "@@ -0,0 +1 @@\n+x"},
                 {"filename": "b.py", "status": "modified",
                  "patch": "@@ -13,1 +13,1 @@\n-old()\n+new()"}]
        record = {"repo": "platform", "number": 4, "url": "u4", "rounds": 1, "threads": [
            {"severity": "major", "round": 1, "outcome": "not an issue", "path": "scripts/a.py", "url": "t"}],
            "local": local_findings(SECTION)}
        blames = []

        def blame(path):
            blames.append(path)
            return ranges
        got = escapes("platform", {"number": 9, "url": "u9", "mergedAt": "2026-10-10T00:00:00Z"}, files,
                      blame, lambda n: record if n == 4 else None)
        self.assertEqual(blames, ["scripts/a.py", "b.py"])  # an added file has no earlier lines
        self.assertEqual(len(got), 1)
        e = got[0]
        self.assertEqual((e["fix"], e["pr"], e["lines"], e["paths"]), ("platform#9", "platform#4", 1, ["scripts/a.py"]))
        self.assertEqual(e["findings"], ["CI round 1 major: not an issue", "local R1.1 major: fixed",
                                         "local R1.2 critical re-graded minor: issue"])
        s = score([], got + [dict(got[0], pr="platform#5")])
        self.assertEqual((s["fixes"], len(s["escapes"])), (1, 2))  # one fix, two reviewed PRs behind it
        text = body("2026-10", s)
        self.assertIn("| 1 (2 reviewed PR(s) behind them) |", text)
        self.assertIn("[platform#9](u9) changes 1 line(s) [platform#4](u4) last changed; its findings", text)
        self.assertNotIn("scripts/a.py", text)  # no file paths in the public issue (design 6.2)

    def test_an_open_finding_behind_an_escape_is_counted_not_named(self):
        ranges = [{"startingLine": 1, "endingLine": 99, "commit": {"associatedPullRequests": {"nodes": [
            {"number": 4, "url": "u4", "mergedAt": "2026-10-01T00:00:00Z"}]}}}]
        record = {"repo": "platform", "number": 4, "url": "u4", "rounds": 1, "local": [], "threads": [
            {"severity": "critical", "round": 1, "outcome": None, "path": "a.py", "url": "t"},
            {"severity": "major", "round": 1, "outcome": "not an issue", "disputed": True, "path": "a.py", "url": "t"}]}
        got = escapes("platform", {"number": 9, "url": "u9", "mergedAt": "2026-10-10T00:00:00Z"},
                      [{"filename": "a.py", "status": "modified", "patch": "@@ -5,1 +5,1 @@\n-x\n+y"}],
                      lambda path: ranges, lambda n: record)
        self.assertEqual(got[0]["findings"], ["2 still open"])


if __name__ == "__main__":
    unittest.main()

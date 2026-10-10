"""Tests for scripts/ai_scorecard.py. Run: python3 -m unittest discover tests"""

import datetime
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from ai_scorecard import (body, commented_line, cost, last_month, month_range, outcome,  # noqa: E402
                          pr_record, score, severity)

MARKER = "<!-- ai-review -->"
USAGE = '<!-- ai-review-usage: {"model": "claude-opus-5-5", "input": 1000000, "cache_read": 0, ' \
        '"cache_write": 0, "output": 100000} -->'


def review(id, at, commit, text="Summary.", login="github-actions"):
    return {"id": id, "author": {"login": login}, "submittedAt": at, "commit": {"oid": commit},
            "body": f"**AI review (advisory), round 1**\n\n{text}\n\n{USAGE}"}


def thread(review_id, head, line, replies=(), resolved=True, path="a.py"):
    comments = [{"author": {"login": "github-actions"}, "body": f"{head}\n\nx\n\n{MARKER}",
                 "url": f"u/{review_id}/{line}", "diffHunk": f"@@ -1 +1 @@\n+{line}",
                 "pullRequestReview": {"id": review_id}}]
    comments += [{"author": {"login": "someone"}, "body": r, "url": "", "diffHunk": "",
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

    def test_the_commented_line_ends_the_hunk(self):
        self.assertEqual(commented_line("@@ -1,2 +1,2 @@\n a\n+  return total(x)"), "return total(x)")
        self.assertEqual(commented_line(""), "")

    def test_cost_at_the_price_table(self):
        self.assertAlmostEqual(cost({"model": "claude-opus-5-5", "input": 1_000_000, "output": 100_000,
                                     "cache_read": 1_000_000, "cache_write": 0}), 4 + 2 + 0.2)
        self.assertIsNone(cost({"model": "unknown"}))


class RecordTest(unittest.TestCase):
    def data(self):
        old = "x = compute_everything()"
        return {"reviews": {"nodes": [
            review("r2", "2026-10-11T02:00:00Z", "c2"), review("r1", "2026-10-11T01:00:00Z", "c1"),
            review("note", "2026-10-11T03:00:00Z", "c3", "The review was cut off (max_tokens)"),
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
            return "x = compute_everything()\n"
        rec = pr_record("platform", 7, "u", self.data(), read)
        self.assertEqual(rec["rounds"], 2)  # the cut-off note and someone else's review aren't rounds
        self.assertEqual(reads, [("platform", "c1", "a.py")])  # round one's commit, read once
        self.assertAlmostEqual(rec["cost"], 3 * 6.0)  # every review cost tokens, the note too
        t = rec["threads"]
        self.assertEqual([x["round"] for x in t], [1, 2, 2, 2, 1])
        self.assertEqual([x["late"] for x in t], [False, True, False, False, False])
        self.assertEqual([x["outcome"] for x in t], ["fixed", "not an issue", None, None, None])

    def test_a_pr_the_review_never_ran_on(self):
        self.assertIsNone(pr_record("platform", 8, "u", {"reviews": {"nodes": []},
                                                         "reviewThreads": {"nodes": []}}))

    def test_the_score_and_report(self):
        rec = pr_record("platform", 7, "u", self.data(), lambda *a: "x = compute_everything()")
        s = score([rec])
        self.assertEqual(len(s["late"]), 1)
        self.assertEqual(len(s["false_alarms"]), 1)
        self.assertEqual(s["serious"], 3)
        self.assertEqual(s["unrated"], 1)
        self.assertEqual(len(s["unreadable"]), 2)  # resolved with no reply, and with "ok"
        text = body("2026-10", s)
        self.assertIn("| 1 of 3 |", text)
        self.assertIn("1 thread(s) from the reviewer before 2026-10-10", text)
        self.assertIn("### Done when", text)  # a Task form, so issue-fields can file it


if __name__ == "__main__":
    unittest.main()

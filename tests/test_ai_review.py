"""Tests for scripts/ai_review.py (design 6.4). Run: python3 -m unittest discover tests"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
from ai_review import (added_lines, fingerprint, last_fingerprint, review_input, split_diff,  # noqa: E402
                       to_github)

DIFF = """diff --git a/src/a.ts b/src/a.ts
--- a/src/a.ts
+++ b/src/a.ts
@@ -1,3 +1,4 @@
 const a = 1;
-const b = 2;
+const b = 3;
+const c = 4;
 export { a };
diff --git a/pnpm-lock.yaml b/pnpm-lock.yaml
--- a/pnpm-lock.yaml
+++ b/pnpm-lock.yaml
@@ -1 +1 @@
-x
+y
"""


class DiffTest(unittest.TestCase):
    def test_lock_and_built_files_are_left_out(self):
        self.assertEqual(list(split_diff(DIFF)), ["src/a.ts"])

    def test_added_lines_are_numbered_in_the_new_file(self):
        self.assertEqual(added_lines(split_diff(DIFF)["src/a.ts"]), {2, 3})

    def test_an_added_line_starting_with_plus_plus_counts(self):
        diff = ("diff --git a/a.c b/a.c\n--- a/a.c\n+++ b/a.c\n@@ -1,1 +1,3 @@\n x\n"
                "+++i;\n+y\n")
        self.assertEqual(added_lines(diff), {2, 3})

    def test_no_newline_marker_is_not_a_line(self):
        diff = ("diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1,2 +1,3 @@\n x\n-y\n"
                "\\ No newline at end of file\n+y\n+z\n")
        self.assertEqual(added_lines(diff), {2, 3})

    def test_a_large_diff_is_cut_by_whole_files(self):
        files = {"a": "x" * 150_000, "b": "y" * 100_000}
        text, left_out = review_input(files)
        self.assertEqual((len(text), left_out), (150_000, ["b"]))


class PayloadTest(unittest.TestCase):
    def test_comments_on_added_lines_go_inline_the_rest_in_the_body(self):
        files = split_diff(DIFF)
        review = {"summary": "One bug.", "comments": [
            {"path": "src/a.ts", "line": 2, "body": "b changed"},
            {"path": "src/a.ts", "line": 1, "body": "not added"},
            {"path": "nope.ts", "line": 1, "body": "no file"}]}
        out = to_github(review, files, ["big.ts"])
        self.assertEqual(out["event"], "COMMENT")
        self.assertEqual(out["comments"], [{"path": "src/a.ts", "line": 2, "side": "RIGHT",
                                            "body": "b changed\n\n<!-- ai-review -->"}])
        self.assertIn("`src/a.ts:1`: not added", out["body"])
        self.assertIn("`nope.ts:1`", out["body"])
        self.assertIn("Not reviewed (diff too large): `big.ts`", out["body"])
        self.assertTrue(out["body"].startswith("**AI review (advisory)**"))

    def test_a_line_already_commented_on_is_not_repeated(self):
        files = split_diff(DIFF)
        review = {"summary": "s", "comments": [{"path": "src/a.ts", "line": 2, "body": "again"}]}
        out = to_github(review, files, [], already={("src/a.ts", 2)})
        self.assertEqual(out["comments"], [])
        self.assertNotIn("again", out["body"])

    def test_it_never_approves(self):
        self.assertEqual(to_github({"summary": "fine", "comments": []}, {}, [])["event"], "COMMENT")


class FingerprintTest(unittest.TestCase):
    def test_moved_lines_keep_the_fingerprint(self):
        # bringing main in shifts the PR's hunks; the diff itself is the same
        moved = DIFF.replace("@@ -1,3 +1,4 @@", "@@ -40,3 +40,4 @@")
        self.assertEqual(fingerprint(split_diff(DIFF)), fingerprint(split_diff(moved)))

    def test_a_changed_line_changes_it(self):
        self.assertNotEqual(fingerprint(split_diff(DIFF)),
                            fingerprint(split_diff(DIFF.replace("const c = 4;", "const c = 5;"))))

    def test_a_new_claude_md_changes_it(self):
        self.assertNotEqual(fingerprint(split_diff(DIFF), "rules"), fingerprint(split_diff(DIFF), "new rules"))

    def test_the_latest_own_review_counts(self):
        fp = "a" * 40
        bot = {"login": "github-actions[bot]"}
        reviews = [{"user": bot, "body": f"<!-- ai-review-diff: {'b' * 40} -->"},
                   {"user": bot, "body": f"<!-- ai-review-diff: {fp} -->"},
                   {"user": bot, "body": "a review with no fingerprint"}]
        self.assertEqual(last_fingerprint(reviews), fp)

    def test_a_marker_from_anyone_else_is_ignored(self):
        reviews = [{"user": {"login": "someone"}, "body": f"<!-- ai-review-diff: {'a' * 40} -->"}]
        self.assertIsNone(last_fingerprint(reviews))

if __name__ == "__main__":
    unittest.main()

"""Tests for scripts/ai_review.py (design 6.4). Run: python3 -m unittest discover tests"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
from ai_review import (CutOff, added_lines, design_sections, fingerprint, full_files,  # noqa: E402
                       heading, last_fingerprint, parse, request, review_input, split_diff,
                       strip_markers, to_github)

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
    def test_critical_and_major_on_added_lines_are_threads_the_rest_in_the_body(self):
        files = split_diff(DIFF)
        f = {"confidence": "high", "design": "", "failure": ""}
        review = {"summary": "One bug.", "comments": [
            {**f, "path": "src/a.ts", "line": 2, "severity": "major", "body": "b changed",
             "failure": "b is 3, callers expect 2"},
            {**f, "path": "src/a.ts", "line": 3, "severity": "minor", "body": "small"},
            {**f, "path": "src/a.ts", "line": 3, "severity": "nit", "body": "polish"},
            {**f, "path": "src/a.ts", "line": 1, "severity": "critical", "body": "not added"},
            {**f, "path": "nope.ts", "line": 1, "severity": "major", "body": "no file"}]}
        out = to_github(review, files, ["big.ts"], round_no=2)
        self.assertEqual(out["event"], "COMMENT")
        self.assertEqual(out["comments"], [{"path": "src/a.ts", "line": 2, "side": "RIGHT", "body":
                                            "**Major** · high confidence\n\nb changed\n\n"
                                            "**Fails when:** b is 3, callers expect 2\n\n<!-- ai-review -->"}])
        body = out["body"]
        self.assertTrue(body.startswith("**AI review (advisory), round 2**"))
        self.assertIn("Critical, not on a changed line:\n- `src/a.ts:1`", body)
        self.assertIn("Major, not on a changed line:\n- `nope.ts:1`", body)
        self.assertIn("Minor:\n- `src/a.ts:3` (**Minor** · high confidence): small", body)
        self.assertIn("Nits:\n- `src/a.ts:3`", body)
        self.assertLess(body.index("Critical"), body.index("Minor"))
        self.assertIn("Not reviewed (diff too large): `big.ts`", body)

    def test_a_finding_without_a_known_severity_is_minor(self):
        out = to_github({"summary": "s", "comments": [{"path": "src/a.ts", "line": 2, "body": "x"}]},
                        split_diff(DIFF), [])
        self.assertEqual(out["comments"], [])
        self.assertIn("Minor:", out["body"])

    def test_heading_names_the_design_section_once(self):
        c = {"severity": "critical", "confidence": "low", "design": "design 6.2"}
        self.assertEqual(heading(c), "**Critical** · low confidence · design 6.2")

    def test_a_line_already_commented_on_is_not_repeated(self):
        files = split_diff(DIFF)
        review = {"summary": "s", "comments": [{"path": "src/a.ts", "line": 2, "severity": "major",
                                                "body": "again"}]}
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

    def test_a_whitespace_change_changes_it(self):
        # indentation is code in Python and YAML
        self.assertNotEqual(fingerprint(split_diff(DIFF)),
                            fingerprint(split_diff(DIFF.replace("+const c = 4;", "+  const c = 4;"))))

    def test_a_marker_split_around_another_is_stripped(self):
        inner = f"<!-- ai-review-diff: {'a' * 40} -->"
        nested = f"<!-- ai-review-diff: {'b' * 20}{inner}{'b' * 20} -->"
        self.assertEqual(strip_markers(f"x {nested} y"), "x  y")

    def test_a_new_claude_md_changes_it(self):
        self.assertNotEqual(fingerprint(split_diff(DIFF), "rules"), fingerprint(split_diff(DIFF), "new rules"))

    def test_the_latest_own_review_counts(self):
        fp = "a" * 40
        bot = {"login": "github-actions[bot]"}
        reviews = [{"user": bot, "body": f"<!-- ai-review-diff: {'b' * 40} -->"},
                   {"user": bot, "body": f"<!-- ai-review-diff: {fp} -->"},
                   {"user": bot, "body": "a review with no fingerprint"}]
        self.assertEqual(last_fingerprint(reviews), fp)

    def test_the_last_marker_in_a_review_counts(self):
        bot = {"login": "github-actions[bot]"}
        body = f"summary <!-- ai-review-diff: {'b' * 40} --> end\n\n<!-- ai-review-diff: {'a' * 40} -->"
        self.assertEqual(last_fingerprint([{"user": bot, "body": body}]), "a" * 40)

    def test_a_marker_from_anyone_else_is_ignored(self):
        reviews = [{"user": {"login": "someone"}, "body": f"<!-- ai-review-diff: {'a' * 40} -->"}]
        self.assertIsNone(last_fingerprint(reviews))


class RequestTest(unittest.TestCase):
    DESIGN = "## 3. Products\nthree\n## 4. Repository\nfour\n### 4.1 Sub\nsub\n## 6. Engineering\nsix\n## 7. Peras\nseven\n"

    def test_each_repo_reads_its_design_sections(self):
        self.assertEqual(design_sections(self.DESIGN, "platform"),
                         "## 4. Repository\nfour\n### 4.1 Sub\nsub\n## 6. Engineering\nsix\n")
        self.assertIn("## 7. Peras", design_sections(self.DESIGN, "peras"))
        self.assertNotIn("## 3.", design_sections(self.DESIGN, "peras"))

    def test_the_shared_part_is_cached_and_a_later_round_says_so(self):
        system, user = request("rules", "design", "t", "d", {"a.py": "x = 1"}, "diff")
        self.assertEqual(system[1]["cache_control"], {"type": "ephemeral", "ttl": "1h"})
        self.assertIn('<file path="a.py">\nx = 1\n</file>', user)
        self.assertNotIn("This is a later round", system[0]["text"])
        system, user = request("rules", "design", "t", "d", {}, "diff", later=("earlier", "since"))
        self.assertIn("This is a later round", system[0]["text"])
        self.assertIn("<earlier_review>\nearlier\n</earlier_review>", user)
        self.assertIn("<changes_since>\nsince\n</changes_since>", user)

    def test_full_files_leave_out_the_largest_past_the_limit(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            for name, size in (("small", 10), ("big", 399_995)):
                with open(os.path.join(d, name), "w") as f:
                    f.write("x" * size)
            self.assertEqual(list(full_files(["small", "big", "deleted"], d)), ["small"])


class CutOffTest(unittest.TestCase):
    def test_a_finished_review_is_read(self):
        self.assertEqual(parse("end_turn", ['{"summary": "ok", ', '"comments": []}']),
                         {"summary": "ok", "comments": []})

    def test_a_review_cut_off_is_not(self):
        # it must record no fingerprint, so the diff is reviewed again (.github#87)
        with self.assertRaises(CutOff):
            parse("max_tokens", ['{"summary": "o'])
        with self.assertRaises(CutOff):
            parse("end_turn", ["not json"])


if __name__ == "__main__":
    unittest.main()

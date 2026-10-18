"""Tests for scripts/ai_review.py (design 6.4). Run: python3 -m unittest discover tests"""

import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import ai_review  # noqa: E402
from ai_review import (NO_DESIGN, CutOff, added_lines, design_sections, fingerprint,  # noqa: E402
                       full_files, heading, last_fingerprint, parse, read_design, request,
                       review_input, split_diff, strip_markers, to_github)

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
                                            "**Fails when:** b is 3, callers expect 2\n\n"
                                            f"{ai_review.OUTCOME_ASK}\n\n<!-- ai-review -->"}])
        body = out["body"]
        self.assertTrue(body.startswith("**AI review (advisory), round 2**"))
        self.assertIn("Critical, not on a changed line:\n- `src/a.ts:1`", body)
        self.assertIn("Major, not on a changed line:\n- `nope.ts:1`", body)
        self.assertIn("Minor:\n- `src/a.ts:3` (**Minor** · high confidence): small", body)
        self.assertIn("Nits:\n- `src/a.ts:3`", body)
        self.assertLess(body.index("Critical"), body.index("Minor"))
        self.assertIn("Not reviewed (diff too large): `big.ts`", body)

    def test_a_long_body_keeps_the_most_severe_and_says_how_many_were_left_out(self):
        f = {"path": "x.ts", "line": 1, "confidence": "high", "design": "", "failure": "", "body": "b" * 1000}
        review = {"summary": "s", "comments": [{**f, "severity": "nit"}] * 40 + [{**f, "severity": "major"}] * 40}
        body = to_github(review, {}, [])["body"]
        self.assertLess(len(body), 65_536)
        self.assertEqual(body.count("(**Major**"), 40)
        self.assertIn("more finding(s) left out", body)

    def test_a_finding_without_a_known_severity_is_minor(self):
        out = to_github({"summary": "s", "comments": [{"path": "src/a.ts", "line": 2, "body": "x"}]},
                        split_diff(DIFF), [])
        self.assertEqual(out["comments"], [])
        self.assertIn("Minor:", out["body"])

    def test_heading_names_the_design_section_once(self):
        c = {"severity": "critical", "confidence": "low", "design": "design 6.2"}
        self.assertEqual(heading(c), "**Critical** · low confidence · design 6.2")

    def test_design_text_other_than_a_section_number_is_not_posted(self):
        # the design is private; .github is public
        c = {"severity": "major", "confidence": "high", "design": "6.2: the lane merges one PR at a time"}
        self.assertEqual(heading(c), "**Major** · high confidence")

    def test_a_line_already_commented_on_is_not_repeated(self):
        files = split_diff(DIFF)
        review = {"summary": "s", "comments": [{"path": "src/a.ts", "line": 2, "severity": "major",
                                                "body": "again"}]}
        out = to_github(review, files, [], already={("src/a.ts", 2)})
        self.assertEqual(out["comments"], [])
        self.assertNotIn("again", out["body"])

    def test_the_ask_names_the_severity_after_re_grading(self):
        # design 6.4: a reply gives the outcome and the severity after re-grading (.github#201)
        self.assertIn("severity after re-grading", ai_review.OUTCOME_ASK)
        self.assertIn("**Fixed (major, kept)**", ai_review.OUTCOME_ASK)
        # a thread from before keeps its earlier ask, which is still left out of what the model reads
        old = "**Major**\n\nx\n\n" + ai_review.OUTCOME_ASKS[1] + "\n\n<!-- ai-review -->"
        self.assertEqual(ai_review.without_ask(old.replace("<!-- ai-review -->", "")).strip(), "**Major**\n\nx")

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
        bot = {"login": ai_review.REVIEWER}
        reviews = [{"user": bot, "body": f"<!-- ai-review-diff: {'b' * 40} -->"},
                   {"user": bot, "body": f"<!-- ai-review-diff: {fp} -->"},
                   {"user": bot, "body": "a review with no fingerprint"}]
        self.assertEqual(last_fingerprint(reviews), fp)

    def test_the_last_marker_in_a_review_counts(self):
        bot = {"login": ai_review.REVIEWER}
        body = f"summary <!-- ai-review-diff: {'b' * 40} --> end\n\n<!-- ai-review-diff: {'a' * 40} -->"
        self.assertEqual(last_fingerprint([{"user": bot, "body": body}]), "a" * 40)

    def test_a_marker_from_anyone_else_is_ignored(self):
        reviews = [{"user": {"login": "someone"}, "body": f"<!-- ai-review-diff: {'a' * 40} -->"}]
        self.assertIsNone(last_fingerprint(reviews))
        # any workflow can post as github-actions[bot], which posted reviews before the app (.github#225)
        self.assertEqual(ai_review.REVIEWER, "akenon-studio-ai-review[bot]")
        legacy = [{"user": {"login": "github-actions[bot]"}, "body": f"<!-- ai-review-diff: {'a' * 40} -->"}]
        self.assertIsNone(last_fingerprint(legacy))


class RequestTest(unittest.TestCase):
    DESIGN = "## 3. Products\nthree\n## 4. Repository\nfour\n### 4.1 Sub\nsub\n## 6. Engineering\nsix\n## 7. Peras\nseven\n"

    def test_each_repo_reads_its_design_sections(self):
        self.assertEqual(design_sections(self.DESIGN, "platform"),
                         "## 4. Repository\nfour\n### 4.1 Sub\nsub\n## 6. Engineering\nsix\n")
        self.assertIn("## 7. Peras", design_sections(self.DESIGN, "peras"))
        self.assertNotIn("## 3.", design_sections(self.DESIGN, "peras"))

    def test_the_design_repo_reads_all_of_it_as_the_pr_leaves_it(self):
        import os
        self.assertEqual(design_sections(self.DESIGN, "handbook"), self.DESIGN)
        with tempfile.TemporaryDirectory() as pr, tempfile.NamedTemporaryFile("w", suffix=".md") as main:
            main.write("main's design")
            main.flush()
            # no design.md in the PR's checkout: main's
            self.assertEqual(read_design(main.name, "handbook", root=pr), "main's design")
            with open(os.path.join(pr, "design.md"), "w") as f:
                f.write("the PR's design")
            self.assertEqual(read_design(main.name, "handbook", root=pr), "the PR's design")
            # other repos never read a design.md of their own
            self.assertEqual(read_design(main.name, "platform", root=pr), NO_DESIGN)
            # an emptied one: main's
            with open(os.path.join(pr, "design.md"), "w") as f:
                f.write("\n")
            self.assertEqual(read_design(main.name, "handbook", root=pr), "main's design")
            # a design.md that is a symlink (to a runner file, say) isn't read
            os.remove(os.path.join(pr, "design.md"))
            os.symlink("/etc/hosts", os.path.join(pr, "design.md"))
            self.assertNotIn("localhost", read_design(main.name, "handbook", root=pr))
        self.assertEqual(ai_review.review_paths(["design.md", "x.md"], "handbook"), ["x.md"])
        self.assertEqual(ai_review.review_paths(["design.md"], "platform"), ["design.md"])

    def test_the_design_repo_is_reviewed_as_documents(self):
        docs, code = ai_review.instructions("handbook"), ai_review.instructions("platform")
        self.assertEqual(code, ai_review.INSTRUCTIONS)
        self.assertIn("Appendix A", docs)
        self.assertNotIn("Wasted performance", docs)
        self.assertIn("in a workflow or script the PR changes", docs)  # handbook has workflows too
        # the shared parts stay: the opening, the severity scale and the finding format
        self.assertTrue(docs.startswith(code.split("Look for:")[0]))
        self.assertEqual(docs.count("Severity is absolute."), 1)
        self.assertTrue(docs.endswith(code.split("\n\nEach finding:")[1]))
        # the document meanings come after the code scale they replace
        self.assertGreater(docs.index("these meanings replace"), docs.index("- critical: harm"))
        system, _ = request("", "design", "t", "d", {}, "diff", repo="handbook")
        self.assertEqual(system[0]["text"], docs)
        self.assertNotIn("cache_control", system[1], "the PR's own design changes on most pushes")

    def test_the_shared_part_is_cached_and_a_later_round_says_so(self):
        system, user = request("rules", "design", "t", "d", {"a.py": "x = 1"}, "diff")
        self.assertEqual(system[1]["cache_control"], {"type": "ephemeral", "ttl": "1h"})
        self.assertIn('<file path="a.py">\nx = 1\n</file>', user)
        self.assertNotIn("This is a later round", user)
        first = system
        system, user = request("rules", "design", "t", "d", {}, "diff", later=("earlier", "since"))
        self.assertEqual(system, first, "the cached prefix is the same in every round")
        self.assertTrue(user.startswith("This is a later round"))
        self.assertIn("<earlier_review>\nearlier\n</earlier_review>", user)
        self.assertIn("<changes_since>\nsince\n</changes_since>", user)

    def test_full_files_leave_out_the_largest_past_the_limit(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            for name, size in (("small", 10), ("big", 399_995)):
                with open(os.path.join(d, name), "w") as f:
                    f.write("x" * size)
            whole, omitted = full_files(["small", "big", "deleted"], d, budget=400_000)
            self.assertEqual((list(whole), omitted), (["small"], ["big"]))

    def test_files_a_later_round_touched_are_left_out_last(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as d:
            for name, size in (("touched.py", 50), ("untouched.py", 10)):
                with open(os.path.join(d, name), "w") as f:
                    f.write("x" * size)
            found, omitted = ai_review.full_files(["touched.py", "untouched.py"], root=d, budget=55,
                                                  keep={"touched.py"})
            self.assertEqual((list(found), omitted), (["touched.py"], ["untouched.py"]))

    def test_a_symlink_is_not_followed(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as d, tempfile.NamedTemporaryFile("w") as secret:
            secret.write("private")
            secret.flush()
            os.symlink(secret.name, os.path.join(d, "link"))
            with open(os.path.join(d, "real"), "w") as f:
                f.write("code")
            self.assertEqual(full_files(["link", "real", "../x"], d)[0], {"real": "code"})

    def test_a_full_review_after_earlier_rounds_sees_them(self):
        _, user = request("", "", "t", "d", {}, "diff", history="F1 settled")
        self.assertIn("<earlier_review>\nF1 settled\n</earlier_review>", user)
        self.assertFalse(user.startswith("This is a later round"))

    def test_a_later_round_sends_no_full_diff(self):
        _, user = request("", "", "t", "d", {}, "", later=("earlier", "since"))
        self.assertNotIn("<diff>", user)

    def test_omitted_files_are_named_to_the_model(self):
        _, user = request("", "", "t", "d", {}, "diff", omitted=["big.ts"])
        self.assertIn("<files_omitted>\nbig.ts\n</files_omitted>", user)

    def test_a_missing_or_empty_design_says_so(self):
        import tempfile, os
        self.assertEqual(read_design(None, "platform"), NO_DESIGN)
        self.assertEqual(read_design("/nonexistent/design.md", "platform"), NO_DESIGN)
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write("# Design\nno numbered sections\n")
        self.assertEqual(read_design(f.name, "platform"), NO_DESIGN)
        os.unlink(f.name)


class GeneratedFilesTest(unittest.TestCase):
    def test_changed_lockfiles_are_named_not_hidden(self):
        # .github#114: the review called a changed pnpm-lock.yaml missing
        diff = ("diff --git a/package.json b/package.json\n+x\n"
                "diff --git a/pnpm-lock.yaml b/pnpm-lock.yaml\n+y\n"
                "diff --git a/apps/x/dist/a.js b/apps/x/dist/a.js\ndeleted file mode 100644\n-z\n")
        self.assertEqual(ai_review.generated_paths(diff), ["pnpm-lock.yaml", "apps/x/dist/a.js (deleted)"])
        self.assertEqual(ai_review.generated_paths(None), [])
        _, user = ai_review.request("", "", "t", "", {}, "d", generated=["pnpm-lock.yaml"])
        self.assertIn("<generated_files>\npnpm-lock.yaml\n</generated_files>", user)
        self.assertIn("<generated_files>", ai_review.INSTRUCTIONS)

    def test_a_long_list_is_capped(self):
        diff = "".join(f"diff --git a/dist/{i}.js b/dist/{i}.js\n+x\n" for i in range(ai_review.MAX_GENERATED + 5))
        got = ai_review.generated_paths(diff)
        self.assertEqual(len(got), ai_review.MAX_GENERATED + 1)
        self.assertEqual(got[-1], "... and 5 more")


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


class MainTest(unittest.TestCase):
    """main() with GitHub and Claude faked, in a temporary git repo."""

    def setUp(self):
        import os, subprocess, tempfile
        self.cwd, self.dir = os.getcwd(), tempfile.mkdtemp()
        os.chdir(self.dir)
        subprocess.run(["git", "init", "-q"], check=True)
        self.posted, self.saved = [], {k: getattr(ai_review, k) for k in
                                       ("github", "all_reviews", "ask_claude", "earlier_comments")}
        self.env = {k: os.environ.get(k) for k in ("GITHUB_TOKEN", "GITHUB_REPOSITORY", "PR_NUMBER", "DESIGN_FILE")}
        os.environ.update(GITHUB_TOKEN="t", GITHUB_REPOSITORY="akenon-studio/platform", PR_NUMBER="7")
        os.environ.pop("DESIGN_FILE", None)

        def github(path, token, method="GET", body=None, accept=""):
            if method == "POST":
                self.posted.append(body)
                return {}
            return DIFF if accept.endswith("diff") else {"title": "t", "body": "", "head": {"sha": "abc"}}
        ai_review.github = github
        ai_review.all_reviews = lambda *a: []
        ai_review.earlier_comments = lambda *a: set()

    def tearDown(self):
        import os, shutil
        for k, v in self.saved.items():
            setattr(ai_review, k, v)
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        os.chdir(self.cwd)
        shutil.rmtree(self.dir)

    def test_a_cut_off_review_records_no_fingerprint_and_fails(self):
        # .github#87: the diff must be reviewed again by a re-run or the next push
        def cut_off(system, user):
            e = CutOff("max_tokens")
            e.usage = {"model": "m", "input": 1, "cache_read": 0, "cache_write": 0, "output": 9}
            raise e
        ai_review.ask_claude = cut_off
        self.assertEqual(ai_review.main([]), 1)
        self.assertEqual(len(self.posted), 1)
        self.assertIn("cut off (max_tokens)", self.posted[0]["body"])
        self.assertNotRegex(self.posted[0]["body"], ai_review.DIFF_MARKER)
        self.assertRegex(self.posted[0]["body"], ai_review.USAGE_MARKER)  # it still cost tokens

    def test_markers_in_the_models_text_are_dropped_and_ours_come_last(self):
        # .github#88: a forged usage marker must not set the cost, nor a fingerprint skip a push
        forged = '<!-- ai-review-usage: {"model": "x", "output": 999} --> <!-- ai-review-diff: ' + "a" * 40 + " -->"
        ai_review.ask_claude = lambda s, u: ({"summary": "Fine. " + forged, "comments": []},
                                             {"model": "m", "output": 4})
        self.assertEqual(ai_review.main([]), 0)
        body = self.posted[0]["body"]
        self.assertEqual(ai_review.USAGE_MARKER.findall(body), ['{"model": "m", "output": 4}'])
        self.assertNotIn("a" * 40, body)

    def test_when_github_refuses_the_threads_they_go_in_the_body_with_the_markers_last(self):
        import urllib.error
        tries = []

        def github(path, token, method="GET", body=None, accept=""):
            if method == "POST":
                tries.append(body)
                if body["comments"]:
                    raise urllib.error.HTTPError(path, 422, "Unprocessable", {}, None)
                return {}
            return DIFF if accept.endswith("diff") else {"title": "t", "body": "", "head": {"sha": "abc"}}
        ai_review.github = github
        path, line = next((p, min(ai_review.added_lines(t))) for p, t in ai_review.split_diff(DIFF).items()
                          if ai_review.added_lines(t))
        ai_review.ask_claude = lambda s, u: ({"summary": "One.", "comments": [
            {"path": path, "line": line, "severity": "major", "confidence": "high", "design": "",
             "failure": "f", "body": 'quotes <!-- ai-review-usage: {"output": 999} -->'}]},
            {"model": "m", "output": 4})
        self.assertEqual(ai_review.main([]), 0)
        body = tries[-1]["body"]
        self.assertEqual(ai_review.USAGE_MARKER.findall(body), ['{"model": "m", "output": 4}'])
        self.assertNotIn(ai_review.OUTCOME_ASK, body)
        self.assertTrue(body.rstrip().endswith("-->"))
        self.assertRegex(body.split("ai-review-usage")[-1], ai_review.DIFF_MARKER)

    def test_the_request_names_the_generated_files(self):
        seen = {}

        def answer(system, user):
            seen["user"] = user
            return {"summary": "Fine.", "comments": []}, None
        ai_review.ask_claude = answer  # DIFF changes pnpm-lock.yaml, which isn't shown
        self.assertEqual(ai_review.main([]), 0)
        self.assertIn("<generated_files>\npnpm-lock.yaml\n</generated_files>", seen["user"])

    def test_a_later_round_names_the_generated_files_too(self):
        import subprocess
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x"],
                       check=True)
        head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        ai_review.all_reviews = lambda *a: [{"user": {"login": ai_review.REVIEWER}, "commit_id": head,
                                              "body": "**AI review (advisory), round 1**\n\n<!-- ai-review-diff: " + "a" * 40 + " -->"}]
        saved = ai_review.earlier_review
        ai_review.earlier_review = lambda *a: "earlier findings"
        seen = {}

        def answer(system, user):
            seen["user"] = user
            return {"summary": "Fine.", "comments": []}, None
        ai_review.ask_claude = answer
        try:
            self.assertEqual(ai_review.main([]), 0)
        finally:
            ai_review.earlier_review = saved
        self.assertIn("This is a later round", seen["user"])
        self.assertIn("<generated_files>\npnpm-lock.yaml\n</generated_files>", seen["user"])

    def test_a_finished_review_records_its_fingerprint_and_says_the_design_is_missing(self):
        seen = {}

        def answer(system, user):
            seen["design"] = system[1]["text"]
            return {"summary": "Fine.", "comments": []}, {"model": "m", "input": 1, "cache_read": 2,
                                                           "cache_write": 3, "output": 4}
        ai_review.ask_claude = answer
        self.assertEqual(ai_review.main([]), 0)
        self.assertRegex(self.posted[0]["body"], ai_review.DIFF_MARKER)
        # the scorecard's cost per PR (.github#88)
        self.assertEqual(ai_review.USAGE_MARKER.findall(self.posted[0]["body"]),
                         ['{"model": "m", "input": 1, "cache_read": 2, "cache_write": 3, "output": 4}'])
        self.assertIn(NO_DESIGN, seen["design"])


    def test_a_handbook_pr_is_reviewed_as_documents_against_its_own_design(self):
        import os
        os.environ["GITHUB_REPOSITORY"] = "akenon-studio/handbook"
        with open("design.md", "w") as f:
            f.write("## 6. Engineering\nthe PR's version\n")
        diff = "diff --git a/design.md b/design.md\n--- a/design.md\n+++ b/design.md\n@@ -1 +1,2 @@\n ## 6. Engineering\n+the PR's version\n"
        github = ai_review.github

        def with_design_diff(path, token, method="GET", body=None, accept=""):
            return diff if accept.endswith("diff") else github(path, token, method, body, accept)
        ai_review.github = with_design_diff
        seen = {}

        def answer(system, user):
            seen.update(system=system, user=user)
            return {"summary": "Fine.", "comments": []}, None
        ai_review.ask_claude = answer
        self.assertEqual(ai_review.main([]), 0)
        self.assertEqual(seen["system"][0]["text"], ai_review.instructions("handbook"))
        self.assertIn("the PR's version", seen["system"][1]["text"])
        self.assertNotIn('<file path="design.md">', seen["user"], "sent once, as the design")
        # the design is part of the fingerprint: a change elsewhere in it is a new review
        first = ai_review.DIFF_MARKER.findall(self.posted[-1]["body"])[0]
        with open("design.md", "a") as f:
            f.write("another section changed on main\n")
        self.assertEqual(ai_review.main([]), 0)
        self.assertNotEqual(ai_review.DIFF_MARKER.findall(self.posted[-1]["body"])[0], first)

    def test_cis_later_round_in_handbook_shows_what_changed_in_a_design_from_main(self):
        # .github#192, design 6.4: a later round on what changed in the design, for a PR that
        # doesn't change design.md itself
        import os, subprocess
        g = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
        os.environ["GITHUB_REPOSITORY"] = "akenon-studio/handbook"
        with open("design.md", "w") as f:
            f.write("## 6. Engineering\nas reviewed\n")
        with open("check.py", "w") as f:
            f.write("x = 1\n")
        subprocess.run(g + ["add", "design.md", "check.py"], check=True)
        subprocess.run(g + ["commit", "-qm", "reviewed"], check=True)
        reviewed = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        with open("design.md", "a") as f:
            f.write("a rule changed on main\n")
        subprocess.run(g + ["add", "design.md"], check=True)
        subprocess.run(g + ["commit", "-qm", "main brought in"], check=True)
        diff = "diff --git a/check.py b/check.py\n--- /dev/null\n+++ b/check.py\n@@ -0,0 +1 @@\n+x = 1\n"
        github = ai_review.github
        ai_review.github = lambda path, token, method="GET", body=None, accept="": (
            diff if accept.endswith("diff") else github(path, token, method, body, accept))
        ai_review.all_reviews = lambda *a: [{"user": {"login": ai_review.REVIEWER}, "commit_id": reviewed,
                                              "body": "round 1\n<!-- ai-review-diff: " + "a" * 40 + " -->"}]
        saved = ai_review.earlier_review
        ai_review.earlier_review = lambda *a: "earlier findings"
        seen = {}
        ai_review.ask_claude = lambda system, user: (seen.update(user=user) or {"summary": "Fine.", "comments": []},
                                                     None)
        try:
            self.assertEqual(ai_review.main([]), 0)
            later = seen["user"]
            # the design's diff counts toward "too much changed": past the limit with it alone, a full round
            size = ai_review.MAX_DIFF_CHARS
            ai_review.MAX_DIFF_CHARS = len(diff) + 1  # the PR's diff fits; with main's design diff, it doesn't
            try:
                self.assertEqual(ai_review.main([]), 0)
            finally:
                ai_review.MAX_DIFF_CHARS = size
            self.assertFalse(seen["user"].startswith(ai_review.LATER_ROUND.strip()))
            self.assertNotIn("<design_changed_on_main>", seen["user"])
            seen["user"] = later
        finally:
            ai_review.earlier_review = saved
        self.assertTrue(seen["user"].startswith(ai_review.LATER_ROUND.strip()))
        moved = seen["user"].split("<design_changed_on_main>")[-1]
        self.assertIn("+a rule changed on main", moved)  # what changed in it, not only the new design
        self.assertNotIn("a rule changed on main", seen["user"].split("<changes_since>")[1].split("</changes_since>")[0])
        self.assertIn(ai_review.DESIGN_MOVED_NOTE, seen["user"])  # labelled main's, to check the PR against
        self.assertIn('<file path="check.py">', seen["user"])  # the PR's own files, unchanged since, in full
        self.assertNotIn('<file path="design.md">', seen["user"])  # sent once, as the design
        # outside handbook, the design isn't the repo's own file: nothing is added
        self.assertEqual(ai_review.design_moved(reviewed, "platform", {"check.py": ""}), ("", None))
        # a PR that changes the design, with no base to tell main's part: its diff from its own files
        self.assertEqual(ai_review.design_moved(reviewed, "handbook", {"design.md": ""}), ("", None))

    def design_pr_repo(self, conflict=False, other_conflict=False):
        """A handbook branch that changes the design, reviewed; then main changes another part of it
        (or the same line, with `conflict`), is merged in, and the branch fixes its own text. With
        `other_conflict`, both change notes.md too, which the merge resolves.
        Returns the reviewed commit; main is `main-line` and `origin/main`."""
        import subprocess
        g = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
        # blank lines after the rule: main's diff ends in blank context lines, which must survive
        lines = ["## 6. Engineering", "a rule", "", "", "", *(f"filler {i}" for i in range(8)), "the end"]

        def write(text, message, notes="notes\n"):
            with open("design.md", "w") as f:
                f.write(text)
            with open("notes.md", "w") as f:
                f.write(notes)
            subprocess.run(g + ["add", "design.md", "notes.md"], check=True)
            subprocess.run(g + ["commit", "-qm", message], check=True)
        write("\n".join(lines) + "\n", "base")
        subprocess.run(g + ["branch", "-q", "main-line"], check=True)
        write("\n".join(lines) + "\nthe PR's first text\n", "the PR", "the PR's notes\n" if other_conflict else "notes\n")
        reviewed = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        subprocess.run(g + ["checkout", "-q", "main-line"], check=True)
        on_main = list(lines)
        if conflict:
            on_main.append("main's own last line")
        else:
            on_main[1] = "a rule changed on main"
        write("\n".join(on_main) + "\n", "on main", "main's notes\n" if other_conflict else "notes\n")
        subprocess.run(g + ["update-ref", "refs/remotes/origin/main", "main-line"], check=True)
        subprocess.run(g + ["checkout", "-q", "-"], check=True)
        merged = subprocess.run(g + ["merge", "-q", "--no-edit", "main-line"], capture_output=True)
        text = "\n".join(on_main) + "\nthe PR's first text\n"
        notes = "resolved notes\n" if other_conflict else "notes\n"
        if merged.returncode:  # the conflict, resolved by the author
            with open("design.md", "w") as f:
                f.write(text)
            with open("notes.md", "w") as f:
                f.write(notes)
            subprocess.run(g + ["add", "design.md", "notes.md"], check=True)
            subprocess.run(g + ["commit", "-qm", "merge main", "--no-edit"], check=True)
        write(text + "the PR's fix\n", "the fix", notes)
        return reviewed

    def test_cis_later_round_shows_mains_design_changes_apart_from_a_design_prs_own(self):
        # .github#195, design 6.4: a PR that changes design.md gets main's design edits labelled, not
        # mixed into its own changes since the last review
        import os, subprocess
        os.environ["GITHUB_REPOSITORY"] = "akenon-studio/handbook"
        reviewed = self.design_pr_repo()
        diff = subprocess.run(["git", "diff", "origin/main...HEAD"], capture_output=True, text=True).stdout
        ai_review.github = lambda path, token, method="GET", body=None, accept="": (
            diff if accept.endswith("diff") else {"title": "t", "body": "", "head": {"sha": "abc"},
                                                  "base": {"ref": "main"}})
        ai_review.all_reviews = lambda *a: [{"user": {"login": ai_review.REVIEWER}, "commit_id": reviewed,
                                              "body": "round 1\n<!-- ai-review-diff: " + "a" * 40 + " -->"}]
        saved = ai_review.earlier_review
        ai_review.earlier_review = lambda *a: "earlier findings"
        seen = {}
        ai_review.ask_claude = lambda system, user: (seen.update(user=user) or {"summary": "Fine.", "comments": []},
                                                     None)
        try:
            self.assertEqual(ai_review.main([]), 0)
        finally:
            ai_review.earlier_review = saved
        user = seen["user"]
        self.assertTrue(user.startswith(ai_review.LATER_ROUND.strip()))
        own = user.split("<changes_since>")[1].split("</changes_since>")[0]
        self.assertIn("+the PR's fix", own)
        self.assertNotIn("a rule changed on main", own)  # main's, not the PR's
        self.assertIn("+a rule changed on main", user.split("<design_changed_on_main>")[-1])
        self.assertIn(ai_review.DESIGN_MOVED_NOTE, user)

    def test_a_later_local_run_shows_mains_design_changes_apart_from_a_design_branchs_own(self):
        # .github#195: the local --prompt --since mode, as in CI
        reviewed = self.design_pr_repo()
        with open("earlier.txt", "w") as f:
            f.write("R1.1 MAJOR design.md:1: x\n")
        user = self.prompt("handbook", "main-line", "design.md", "earlier.txt", "--since", reviewed)
        own = user.split("<changes_since>")[1].split("</changes_since>")[0]
        self.assertIn("+the PR's fix", own)
        self.assertNotIn("a rule changed on main", own)
        self.assertIn("+a rule changed on main", user.split("<design_changed_on_main>")[-1])

    def test_conflicting_design_changes_are_shown_mixed_with_mains_labelled(self):
        # when main's design change and the PR's conflict, the PR's can't be told apart: its diff since
        # shows both, and main's is still labelled (the note says its lines can show in both)
        import subprocess
        reviewed = self.design_pr_repo(conflict=True)
        files = ai_review.split_diff(subprocess.run(["git", "diff", "main-line...HEAD"], capture_output=True,
                                                    text=True).stdout)
        since, moved, size = ai_review.later_changes(reviewed, "handbook", files, "main-line")
        self.assertIn("+main's own last line", moved)
        self.assertIn("+the PR's fix", since)
        self.assertIn("main's own last line", since)
        # main's diff is in `since` already: the "too much changed" limit counts it once
        self.assertEqual(size, len(since))

    def test_a_conflict_in_another_file_still_shows_the_design_prs_own_changes_apart(self):
        # the merge's conflict is in notes.md, which the author resolved; the design's merged cleanly
        import subprocess
        reviewed = self.design_pr_repo(other_conflict=True)
        files = ai_review.split_diff(subprocess.run(["git", "diff", "main-line...HEAD"], capture_output=True,
                                                    text=True).stdout)
        since, moved, size = ai_review.later_changes(reviewed, "handbook", files, "main-line")
        self.assertIn("+a rule changed on main", moved)
        self.assertEqual(size, len(since) + len(moved))
        # whole, as git gives it: a hunk's trailing blank context lines kept
        self.assertEqual(moved, subprocess.run(["git", "diff", reviewed + "~1", "main-line", "--", "design.md"],
                                               capture_output=True, text=True).stdout)
        self.assertTrue(moved.endswith(" \n"))
        self.assertIn("+the PR's fix", since)
        self.assertNotIn("a rule changed on main", since)

    def test_a_local_review_stops_when_it_cant_read_the_design(self):
        import subprocess
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x"],
                       check=True)
        with self.assertRaises(SystemExit):
            ai_review.prompt_main("platform", "HEAD", "/nonexistent/design.md")

    def test_a_local_review_gets_the_settled_findings_as_an_earlier_round(self):
        # design 6.4: findings a partner settled don't count against a later local run
        import contextlib, io, json, subprocess
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x"],
                       check=True)
        with open("design.md", "w") as f:
            f.write("# design\n")
        with open("settled.txt", "w") as f:
            f.write("MAJOR a.py:1 settled: not an issue, the partner said why")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(ai_review.prompt_main("handbook", "HEAD", "design.md", "settled.txt"), 0)
        user = json.loads(out.getvalue())["user"]
        self.assertIn("the partner said why", user)
        self.assertIn("Don't repeat them", user)


    def later_run_repo(self):
        """A branch with a.py and b.py, a local run's commit on it, then main moving on (c.py) and
        merged in, a fix to a.py, and a lockfile changed since the run."""
        import subprocess
        g = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
        with open("design.md", "w") as f:
            f.write("# design\n")
        subprocess.run(g + ["commit", "-q", "--allow-empty", "-m", "base"], check=True)
        subprocess.run(g + ["branch", "-q", "main-line"], check=True)
        for name, text in (("a.py", "x = 1\n"), ("b.py", "y = 1\n")):
            with open(name, "w") as f:
                f.write(text)
        subprocess.run(g + ["add", "a.py", "b.py"], check=True)
        subprocess.run(g + ["commit", "-qm", "the branch"], check=True)
        last = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        subprocess.run(g + ["checkout", "-q", "main-line"], check=True)
        with open("c.py", "w") as f:
            f.write("z = 1  # main's\n")
        subprocess.run(g + ["add", "c.py"], check=True)
        subprocess.run(g + ["commit", "-qm", "on main"], check=True)
        subprocess.run(g + ["checkout", "-q", "-"], check=True)
        subprocess.run(g + ["merge", "-q", "--no-edit", "main-line"], check=True)
        with open("a.py", "a") as f:
            f.write("x = 2  # the fix\n")
        with open("pnpm-lock.yaml", "w") as f:
            f.write("lockfileVersion: '9.0'\n" + "noise: 1\n" * 50)
        subprocess.run(g + ["add", "a.py", "pnpm-lock.yaml"], check=True)
        subprocess.run(g + ["commit", "-qm", "fix"], check=True)
        with open("earlier.txt", "w") as f:
            f.write("R1.1 MAJOR a.py:1: x is wrong\n  Outcome: fixed")
        return last

    def prompt(self, *args):
        import contextlib, io, json
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(ai_review.main(["--prompt", *args]), 0)
        return json.loads(out.getvalue())["user"]

    def test_a_later_local_run_gets_only_the_branchs_changes_since_with_the_earlier_findings(self):
        # design 6.4: later local runs review what changed since the last, built as CI's later rounds
        last = self.later_run_repo()
        user = self.prompt("handbook", "main-line", "design.md", "earlier.txt", "--since", last)
        self.assertTrue(user.startswith(ai_review.LATER_ROUND.strip()))
        self.assertIn("<earlier_review>\nR1.1 MAJOR a.py:1: x is wrong", user)
        changes = user.split("<changes_since>")[1]
        self.assertIn("x = 2  # the fix", changes)
        self.assertNotIn("main's", user)  # main's changes aren't the branch's
        self.assertNotIn("noise: 1", changes)  # nor is a lockfile reviewed
        self.assertIn("<generated_files>\npnpm-lock.yaml", user)  # but it is named, as in CI
        self.assertIn('<file path="a.py">', user)
        self.assertNotIn('<file path="b.py">', user)  # untouched since the last run
        self.assertNotIn("<diff>", user)

    def test_a_later_local_run_is_round_one_where_ci_would_be(self):
        import subprocess
        g = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
        last = self.later_run_repo()
        # a rewritten branch: the last run's commit exists, but is no longer in the history
        subprocess.run(g + ["checkout", "-q", "-b", "rewritten", "main-line"], check=True)
        with open("a.py", "w") as f:
            f.write("x = 3\n")
        subprocess.run(g + ["add", "a.py"], check=True)
        subprocess.run(g + ["commit", "-qm", "rewritten"], check=True)
        user = self.prompt("handbook", "main-line", "design.md", "earlier.txt", "--since", last)
        self.assertFalse(user.startswith(ai_review.LATER_ROUND.strip()))
        self.assertIn("<diff>", user)
        # too much changed since: round one, as in CI
        subprocess.run(g + ["checkout", "-q", "-"], check=True)
        saved = ai_review.MAX_DIFF_CHARS
        ai_review.MAX_DIFF_CHARS = 10
        try:
            user = self.prompt("handbook", "main-line", "design.md", "earlier.txt", "--since", last)
            self.assertFalse(user.startswith(ai_review.LATER_ROUND.strip()))
        finally:
            ai_review.MAX_DIFF_CHARS = saved
        # CLAUDE.md changed since, on a branch that descends from the last run: a full round, as in CI
        subprocess.run(g + ["checkout", "-q", "-b", "rule", last], check=True)
        with open("a.py", "a") as f:
            f.write("x = 4\n")
        with open("CLAUDE.md", "w") as f:
            f.write("a new rule\n")
        subprocess.run(g + ["add", "a.py", "CLAUDE.md"], check=True)
        subprocess.run(g + ["commit", "-qm", "rule"], check=True)
        user = self.prompt("handbook", "main-line", "design.md", "earlier.txt", "--since", last)
        self.assertFalse(user.startswith(ai_review.LATER_ROUND.strip()))
        self.assertIn("x = 4", user.split("<diff>")[1])
        # a branch whose own changes since are only generated files: nothing to review, never the whole tree
        subprocess.run(g + ["checkout", "-q", "-b", "lock-only", last], check=True)
        with open("pnpm-lock.yaml", "w") as f:
            f.write("lockfileVersion: '9.0'\n")
        subprocess.run(g + ["add", "pnpm-lock.yaml"], check=True)
        subprocess.run(g + ["commit", "-qm", "lock"], check=True)
        subprocess.run(g + ["merge", "-q", "--no-edit", "main-line"], check=True)
        import contextlib, io, json
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(ai_review.main(["--prompt", "handbook", "main-line", "design.md", "earlier.txt",
                                             "--since", last]), 0)
        self.assertIn("nothing", json.loads(out.getvalue()))  # nothing to review, as CI would skip it
        # in handbook, a design changed on main is a new round all the same, as in CI
        subprocess.run(g + ["checkout", "-q", "main-line"], check=True)
        with open("design.md", "w") as f:
            f.write("# design\nchanged on main\n")
        subprocess.run(g + ["add", "design.md"], check=True)
        subprocess.run(g + ["commit", "-qm", "design on main"], check=True)
        subprocess.run(g + ["checkout", "-q", "lock-only"], check=True)
        subprocess.run(g + ["merge", "-q", "--no-edit", "main-line"], check=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ai_review.main(["--prompt", "handbook", "main-line", "design.md", "earlier.txt", "--since", last])
        request = json.loads(out.getvalue())
        self.assertNotIn("nothing", request)
        self.assertIn("+changed on main", request["user"].split("<design_changed_on_main>")[-1])  # what changed in it
        self.assertIn(ai_review.DESIGN_MOVED_NOTE, request["user"])
        # the design's diff counts toward "too much changed": past the limit with it alone, round one
        own = len(ai_review.changes_since(last, list(ai_review.split_diff(subprocess.run(
            ["git", "diff", "main-line...HEAD"], capture_output=True, text=True).stdout))) or "")
        saved = ai_review.MAX_DIFF_CHARS
        ai_review.MAX_DIFF_CHARS = own + 1  # the branch's own changes fit; with main's design diff, they don't
        try:
            user = self.prompt("handbook", "main-line", "design.md", "earlier.txt", "--since", last)
        finally:
            ai_review.MAX_DIFF_CHARS = saved
        self.assertFalse(user.startswith(ai_review.LATER_ROUND.strip()))
        self.assertNotIn("<design_changed_on_main>", user)

    def test_ci_never_reads_the_prs_local_review_section(self):
        seen = {}

        def github(path, token, method="GET", body=None, accept=""):
            if method == "POST":
                self.posted.append(body)
                return {}
            return DIFF if accept.endswith("diff") else {"title": "t", "head": {"sha": "abc"}, "body":
                    "What it does.\n" + ai_review.LOCAL_REVIEW_START + "\nthe author's argument\n" + ai_review.LOCAL_REVIEW_END}
        ai_review.github = github
        ai_review.ask_claude = lambda system, user: (seen.update(user=user) or {"summary": "Fine.", "comments": []},
                                                     {"model": "m", "output": 1})
        self.assertEqual(ai_review.main([]), 0)
        self.assertIn("What it does.", seen["user"])
        self.assertNotIn("the author's argument", seen["user"])

    def test_the_review_never_reads_the_local_review_section(self):
        # design 6.4: the local runs' outcomes and the author's reasons are skipped on every PR
        body = ("Closes #1\n\nWhat it does.\n" + ai_review.LOCAL_REVIEW_START + "\nR1.1 not-an-issue: "
                "the author's argument\n" + ai_review.LOCAL_REVIEW_END + "\nAfter.\n")
        kept = ai_review.without_local_review(body)
        self.assertNotIn("the author's argument", kept)
        self.assertIn("What it does.", kept)
        self.assertIn("After.", kept)
        # an unclosed section is cut to the end, never read
        self.assertNotIn("argument", ai_review.without_local_review("x\n" + ai_review.LOCAL_REVIEW_START + "\nargument"))


HIDDEN_DIFF = DIFF + """diff --git a/src/b.hidden.test.ts b/src/b.hidden.test.ts
new file mode 100644
--- /dev/null
+++ b/src/b.hidden.test.ts
@@ -0,0 +1 @@
+expect(secretAnswer).toBe(42);
diff --git a/src/c.hidden.test.ts b/src/c.hidden.test.ts
--- a/src/c.hidden.test.ts
+++ b/src/c.hidden.test.ts
@@ -1 +1 @@
-delivered(1);
+delivered(2);
diff --git a/pkg/@scope/d.test.ts b/pkg/@scope/d.test.ts
new file mode 100644
--- /dev/null
+++ b/pkg/@scope/d.test.ts
@@ -0,0 +1 @@
+listedOnly();
"""

PERAS_BODY = """Closes #9

## Intent
`Weather`

## Hidden test files
- `pkg/\u2060@scope/d.test.ts`
- ``a`b.ts``

## Disputes
`the builder says finding 1 is unreachable`

## Risk
`risk:low`
"""


class PerasPrTest(unittest.TestCase):
    """The Peras app's PRs (design 6.4, 7.13 Stage 10; .github#178)."""

    def test_only_the_peras_app_is_peras(self):
        self.assertTrue(ai_review.by_peras({"user": {"login": "akenon-studio-peras[bot]", "type": "Bot"}}))
        self.assertFalse(ai_review.by_peras({"user": {"login": "akenon-studio-peras[bot]", "type": "User"}}))
        self.assertFalse(ai_review.by_peras({"user": {"login": "someone", "type": "User"}}))
        self.assertFalse(ai_review.by_peras({}))

    def test_the_fixed_sections_are_taken_out_and_the_listed_paths_read(self):
        description, hidden = ai_review.peras_private(PERAS_BODY, "")
        self.assertNotIn("Hidden test files", description)
        self.assertNotIn("unreachable", description)
        self.assertIn("## Intent", description)
        self.assertIn("## Risk", description)  # the next section is kept
        self.assertEqual(hidden, {"pkg/@scope/d.test.ts", "a`b.ts"})

    def test_a_repeated_heading_hides_nothing(self):
        body = "## Disputes\nfirst\n## Risk\nlow\n## Disputes\nsecond\n"
        text, _ = ai_review.without_section(body, "## Disputes")
        self.assertEqual(text, "## Risk\nlow\n")

    def test_a_new_file_named_as_a_hidden_test_is_left_out_but_a_delivered_one_is_not(self):
        self.assertEqual(ai_review.added_hidden_tests(HIDDEN_DIFF), {"src/b.hidden.test.ts"})
        renamed = "diff --git a/x.py b/tests/test_hidden_x.py\nnew file mode 100644\n"
        self.assertEqual(ai_review.added_hidden_tests(renamed), {"tests/test_hidden_x.py"})

    def test_a_fenced_heading_doesnt_end_the_disputes(self):
        body = "## Disputes\n```\n## not a heading\nsecret reason\n```\n## Risk\nlow\n"
        text, _ = ai_review.without_section(body, "## Disputes")
        self.assertEqual(text, "## Risk\nlow\n")

    def test_quoted_and_odd_paths_are_read(self):
        quoted = ('diff --git "a/t/caf\\303\\251.hidden.test.ts" "b/t/caf\\303\\251.hidden.test.ts"\n'
                  'new file mode 100644\n--- /dev/null\n+++ "b/t/caf\\303\\251.hidden.test.ts"\n@@ -0,0 +1 @@\n+x\n')
        self.assertEqual(ai_review.added_hidden_tests(quoted), {"t/caf\u00e9.hidden.test.ts"})
        self.assertEqual(ai_review.without_paths(quoted, {"t/caf\u00e9.hidden.test.ts"}), "")
        spaced = ("diff --git a/x b/y.hidden.test.ts b/x b/y.hidden.test.ts\nnew file mode 100644\n"
                  "--- /dev/null\n+++ b/x b/y.hidden.test.ts\t\n@@ -0,0 +1 @@\n+x\n")
        self.assertEqual(ai_review.without_paths(spaced, {"x b/y.hidden.test.ts"}), "")
        renamed = DIFF + ("diff --git a/old.ts b/new.ts\nsimilarity index 90%\nrename from old.ts\n"
                          "rename to new.ts\n")
        self.assertEqual(ai_review.without_paths(renamed, {"old.ts"}), DIFF)

    def test_without_paths_drops_whole_files(self):
        out = ai_review.without_paths(HIDDEN_DIFF, {"src/b.hidden.test.ts", "pkg/@scope/d.test.ts"})
        self.assertNotIn("secretAnswer", out)
        self.assertNotIn("listedOnly", out)
        self.assertIn("delivered(2)", out)
        self.assertIn("const c = 4;", out)
        self.assertIsNone(ai_review.without_paths(None, {"x"}))
        self.assertEqual(ai_review.without_paths(DIFF, set()), DIFF)


class PerasMainTest(unittest.TestCase):
    """main() on a Peras PR and a person's, with GitHub and Claude faked."""

    def setUp(self):
        import os, subprocess, tempfile
        self.cwd, self.dir = os.getcwd(), tempfile.mkdtemp()
        os.chdir(self.dir)
        subprocess.run(["git", "init", "-q"], check=True)
        self.saved = {k: getattr(ai_review, k) for k in
                      ("github", "all_reviews", "ask_claude", "earlier_comments")}
        self.env = {k: os.environ.get(k) for k in ("GITHUB_TOKEN", "GITHUB_REPOSITORY", "PR_NUMBER", "DESIGN_FILE")}
        os.environ.update(GITHUB_TOKEN="t", GITHUB_REPOSITORY="akenon-studio/peras", PR_NUMBER="7")
        os.environ.pop("DESIGN_FILE", None)
        ai_review.all_reviews = lambda *a: []
        ai_review.earlier_comments = lambda *a: set()

    def tearDown(self):
        import os, shutil
        for k, v in self.saved.items():
            setattr(ai_review, k, v)
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        os.chdir(self.cwd)
        shutil.rmtree(self.dir)

    def run_main(self, user):
        posted, seen = [], {}

        def github(path, token, method="GET", body=None, accept=""):
            if method == "POST":
                posted.append(body)
                return {}
            return HIDDEN_DIFF if accept.endswith("diff") else {
                "title": "t", "body": PERAS_BODY, "head": {"sha": "abc"}, "user": user, "draft": True}

        def answer(system, user_text):
            seen["user"] = user_text
            return {"summary": "Fine.", "comments": []}, None
        ai_review.github, ai_review.ask_claude = github, answer
        self.assertEqual(ai_review.main([]), 0)
        return posted, seen.get("user", "")

    def test_a_peras_pr_is_reviewed_without_its_hidden_tests_and_disputes(self):
        posted, user = self.run_main({"login": "akenon-studio-peras[bot]", "type": "Bot"})
        self.assertEqual(len(posted), 1)
        self.assertIn(ai_review.HIDDEN_NOTE, user)  # told they exist, not what they are
        self.assertIn("Left out for a person, as the Peras app's hidden test files: "
                      "`a'b.ts`, `pkg/@scope/d.test.ts`, `src/b.hidden.test.ts`.", posted[0]["body"])
        for private in ("secretAnswer", "b.hidden.test.ts", "listedOnly", "d.test.ts", "unreachable",
                        "Hidden test files"):
            self.assertNotIn(private, user)
        self.assertIn("const c = 4;", user)
        self.assertIn("delivered(2)", user)  # a delivered regression test the builder changed

    def write(self, content):
        import os
        for path in ("src/a.ts", "src/b.hidden.test.ts", "src/c.hidden.test.ts", "pkg/@scope/d.test.ts"):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(f"{content} {'HIDDENCONTENT' if 'a.ts' not in path and 'c.hidden' not in path else 'SHOWN'}\n")

    def commit(self):
        import subprocess
        subprocess.run(["git", "add", "-A"], check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x"], check=True)
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()

    def test_hidden_files_on_disk_are_not_read_in_full(self):
        self.write("one")
        self.commit()
        _, user = self.run_main({"login": "akenon-studio-peras[bot]", "type": "Bot"})
        self.assertIn('<file path="src/a.ts">', user)
        self.assertNotIn("HIDDENCONTENT", user)

    def test_a_later_round_leaves_them_out_too(self):
        self.write("one")
        first = self.commit()
        self.write("two")
        self.commit()
        ai_review.all_reviews = lambda *a: [{"user": {"login": ai_review.REVIEWER}, "commit_id": first,
                                              "body": "**AI review (advisory), round 1**\n\n<!-- ai-review-diff: " + "a" * 40 + " -->"}]
        saved = ai_review.earlier_review
        ai_review.earlier_review = lambda *a: "earlier findings"
        try:
            _, user = self.run_main({"login": "akenon-studio-peras[bot]", "type": "Bot"})
        finally:
            ai_review.earlier_review = saved
        self.assertIn("This is a later round", user)
        self.assertIn("two SHOWN", user)
        self.assertNotIn("HIDDENCONTENT", user)
        self.assertNotIn("b.hidden.test.ts", user)

    def test_the_peras_login_is_the_one_bots_json_lists(self):
        import json
        bots = json.loads((pathlib.Path(ai_review.__file__).resolve().parent.parent / "rulesets"
                           / "bots.json").read_text())
        self.assertIn(ai_review.PERAS_APP.removesuffix("[bot]"), bots["sources"])

    def test_a_later_round_doesnt_read_the_names_back(self):
        posted, _ = self.run_main({"login": "akenon-studio-peras[bot]", "type": "Bot"})
        saved = ai_review.graphql
        ai_review.graphql = lambda *a, **k: {"repository": {"pullRequest": {"reviewThreads": {
            "pageInfo": {"hasNextPage": False}, "nodes": []}}}}
        try:
            text = ai_review.earlier_review("akenon-studio/peras", 7, "t", [{"body": posted[0]["body"]}])
        finally:
            ai_review.graphql = saved
        self.assertIn("Fine.", text)
        self.assertNotIn("b.hidden.test.ts", text)

    def test_a_persons_pr_is_reviewed_whole(self):
        posted, user = self.run_main({"login": "someone", "type": "User"})
        self.assertNotIn(ai_review.HIDDEN_NOTE, user)
        self.assertNotIn("Left out for a person", posted[0]["body"])
        for shown in ("secretAnswer", "listedOnly", "unreachable"):
            self.assertIn(shown, user)

    def test_a_reviewed_draft_marked_ready_with_its_diff_unchanged_gets_no_round(self):
        # The ready_for_review run goes the same way as any: the fingerprint matches, nothing is posted
        files = ai_review.split_diff(ai_review.without_paths(
            HIDDEN_DIFF, ai_review.peras_private(PERAS_BODY, HIDDEN_DIFF)[1]))
        fp = ai_review.fingerprint(files, "")
        ai_review.all_reviews = lambda *a: [{
            "user": {"login": ai_review.REVIEWER}, "commit_id": "abc",
            "body": f"**AI review (advisory), round 1**\n\n<!-- ai-review-diff: {fp} -->"}]
        posted, user = self.run_main({"login": "akenon-studio-peras[bot]", "type": "Bot"})
        self.assertEqual((posted, user), ([], ""))



if __name__ == "__main__":
    unittest.main()

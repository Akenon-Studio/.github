"""Tests for rulesets/sensitive-paths.json and scripts/sensitive_paths.py (design 6.2, 6.4)."""

import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import sensitive_paths as sp  # noqa: E402
from rules import load, managed_repos  # noqa: E402


class FileTest(unittest.TestCase):
    def test_every_pattern_is_in_the_allowed_syntax(self):
        for a in sp.areas():
            for repo, patterns in list(a["paths"].items()) + list(a.get("planned", {}).items()):
                for p in patterns:
                    self.assertTrue(sp.valid(p), f"{a['area']}: {repo}: {p}")

    def test_repos_are_managed_and_teams_exist(self):
        repos, teams = set(managed_repos()), {t["slug"] for t in load("teams.json")["teams"]}
        for a in sp.areas():
            self.assertLessEqual(set(a["paths"]) - {"*"}, repos, a["area"])
            self.assertLessEqual(set(a.get("planned", {})), repos, a["area"])
            if a["codeowners"]:
                self.assertIn(a["team"], teams, a["area"])

    def test_the_design_and_6_2s_areas_are_listed(self):
        names = [a["area"] for a in sp.areas()]
        for area in ("the design", "payments", "accounts and sign-in", "the sandbox and runtime security",
                     "CI and rules config", "Peras"):
            self.assertIn(area, names)
        design = next(a for a in sp.areas() if a["area"] == "the design")
        self.assertFalse(design["codeowners"])  # design 6.4: the design gets runs, not a sign-off


class MatchTest(unittest.TestCase):
    def test_patterns(self):
        self.assertTrue(sp.matches("*", "anything/at/all.ts"))
        self.assertTrue(sp.matches("/.github/", ".github/workflows/ci.yml"))
        self.assertFalse(sp.matches("/.github/", "docs/.github/x"))
        self.assertTrue(sp.matches("/design.md", "design.md"))
        self.assertFalse(sp.matches("/design.md", "docs/design.md"))
        self.assertFalse(sp.matches("/apps/web/src/lib/auth/", "apps/web/src/lib/authz.ts"))

    def test_sensitive_names_the_areas(self):
        hits = sp.sensitive("handbook", ["design.md", "docs/a.md", ".github/workflows/x.yml"])
        self.assertEqual(hits, {"design.md": ["the design"], ".github/workflows/x.yml": ["CI and rules config"]})
        self.assertEqual(sp.sensitive("platform", ["apps/web/src/app/page.tsx"]), {})
        self.assertIn("Peras", sp.sensitive("peras", ["README.md"])["README.md"])


class BlockTest(unittest.TestCase):
    def test_the_design_gets_no_sign_off(self):
        self.assertNotIn("design.md", sp.block("handbook"))
        self.assertIn("/.github/ @akenon-studio/engineers", sp.block("handbook"))

    def test_a_whole_repo_owner_needs_no_other_lines(self):
        self.assertEqual(sp.block("peras").splitlines()[-1], "* @akenon-studio/engineers")
        self.assertNotIn("/.github/", sp.block("peras"))

    def test_this_repos_codeowners_ends_with_its_block(self):
        text = (ROOT / ".github" / "CODEOWNERS").read_text()
        self.assertEqual(sp.codeowners_problems(".github", text), [])

    def test_a_missing_or_edited_block_is_a_problem(self):
        self.assertTrue(sp.codeowners_problems("platform", "/.github/ @akenon-studio/engineers\n"))
        good = "docs/ @akenon-studio/engineers\n\n" + sp.block("platform")
        self.assertEqual(sp.codeowners_problems("platform", good), [])
        self.assertTrue(sp.codeowners_problems("platform", good + "late/ @akenon-studio/design\n"))

    def test_write_keeps_the_hand_lines_and_replaces_the_block(self):
        with tempfile.TemporaryDirectory() as d:
            f = pathlib.Path(d, "CODEOWNERS")
            f.write_text("# Docs owners\ndocs/ @akenon-studio/engineers\n\n" + sp.HEADER
                         + "/.github/ @akenon-studio/engineers\n")  # an older block, part of today's
            sp.write("platform", f)
            text = f.read_text()
            self.assertTrue(text.startswith("# Docs owners\ndocs/ @akenon-studio/engineers\n\n"))
            self.assertEqual(text.count(sp.BEGIN), 1)
            self.assertEqual(sp.codeowners_problems("platform", text), [])

    def test_write_refuses_to_drop_a_hand_line_after_the_block(self):
        with tempfile.TemporaryDirectory() as d:
            f = pathlib.Path(d, "CODEOWNERS")
            f.write_text(sp.HEADER + "/.github/ @akenon-studio/engineers\ndocs/ @akenon-studio/design\n")
            with self.assertRaises(SystemExit) as stop:
                sp.write("platform", f)
            self.assertIn("docs/ @akenon-studio/design", str(stop.exception))
            self.assertIn("docs/ @akenon-studio/design", f.read_text())  # left as it was

    def test_write_refuses_a_hand_line_shaped_like_a_generated_one(self):
        with tempfile.TemporaryDirectory() as d:
            f = pathlib.Path(d, "CODEOWNERS")
            f.write_text(sp.block("platform") + "/docs/ @akenon-studio/design\n")
            with self.assertRaises(SystemExit):
                sp.write("platform", f)

    def test_path_problems(self):
        files = ["apps/web/src/lib/auth/a.ts", "apps/api/src/modules/catalogue/x.ts",
                 "apps/api/src/modules/payouts/p.ts"]
        found = sp.path_problems("platform", files)
        self.assertIn("/packages/runtime/ (the sandbox and runtime security) doesn't exist", found)
        self.assertNotIn("/apps/web/src/lib/auth/ (accounts and sign-in) doesn't exist", found)
        self.assertIn("planned /apps/api/src/modules/payouts/ (payments) now exists: move it to 'paths'", found)
        self.assertNotIn("planned /apps/api/src/modules/purchases/ (payments): its parent folder doesn't exist",
                         found)  # modules/ exists
        self.assertFalse([x for x in found if "review-worker" in x])  # planned, and apps/ exists
        self.assertIn("planned /apps/review-worker/ (the sandbox and runtime security): its parent folder "
                      "doesn't exist", sp.path_problems("platform", ["README.md"]))
        self.assertEqual(sp.path_problems("peras", ["README.md"]), [])  # '*' always exists

    def test_whole_repo_lines_come_first(self):
        lines = sp.block("peras").splitlines()[3:]
        self.assertEqual(lines[0], "* @akenon-studio/engineers")

    def test_planned_paths_are_sensitive_before_they_exist(self):
        self.assertIn("payments", sp.sensitive("platform", ["apps/api/src/modules/payouts/x.ts"])[
            "apps/api/src/modules/payouts/x.ts"])


if __name__ == "__main__":
    unittest.main()

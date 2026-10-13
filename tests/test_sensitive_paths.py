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
            for repo, patterns in a["paths"].items():
                for p in patterns:
                    self.assertTrue(sp.valid(p), f"{a['area']}: {repo}: {p}")

    def test_repos_are_managed_and_teams_exist(self):
        repos, teams = set(managed_repos()), {t["slug"] for t in load("teams.json")["teams"]}
        for a in sp.areas():
            self.assertLessEqual(set(a["paths"]) - {"*"}, repos, a["area"])
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
            f.write_text("# Docs owners\ndocs/ @akenon-studio/engineers\n\n" + sp.HEADER + "/old/ @x/y\n")
            sp.write("platform", f)
            text = f.read_text()
            self.assertTrue(text.startswith("# Docs owners\ndocs/ @akenon-studio/engineers\n\n"))
            self.assertNotIn("/old/", text)
            self.assertEqual(sp.codeowners_problems("platform", text), [])


if __name__ == "__main__":
    unittest.main()

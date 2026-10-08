"""Tests for scripts/pr_author_assigned.py: a PR a person opens has its author among the assignees.
Run: python3 -m unittest discover tests"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from pr_author_assigned import problems  # noqa: E402


class PassesTest(unittest.TestCase):
    def test_author_assigned(self):
        self.assertEqual(problems("RuvinduH", "User", ["RuvinduH"]), [])

    def test_author_among_several(self):
        self.assertEqual(problems("RuvinduH", "User", ["Thytus777", "RuvinduH"]), [])

    def test_logins_compare_without_case(self):
        self.assertEqual(problems("ruvinduh", "User", ["RuvinduH"]), [])

    def test_a_bots_pr_is_exempt(self):
        self.assertEqual(problems("dependabot[bot]", "Bot", []), [])
        self.assertEqual(problems("renovate[bot]", "Bot", ["RuvinduH"]), [])


class FailsTest(unittest.TestCase):
    def test_nobody_assigned(self):
        found = problems("RuvinduH", "User", [])
        self.assertEqual(len(found), 1)
        self.assertIn("@RuvinduH", found[0])
        self.assertIn("--assignee @me", found[0])

    def test_only_someone_else_assigned(self):
        self.assertEqual(len(problems("RuvinduH", "User", ["Thytus777"])), 1)


if __name__ == "__main__":
    unittest.main()

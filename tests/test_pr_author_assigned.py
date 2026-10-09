"""Tests for scripts/pr_author_assigned.py: a PR a person opens has its author among the assignees.
Run: python3 -m unittest discover tests"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from pr_author_assigned import check, problems  # noqa: E402


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


class LateAssigneeTest(unittest.TestCase):
    """handbook#104: `gh pr create --assignee` assigns just after the PR opens."""

    def test_passes_when_the_assignee_arrives_after_the_snapshot(self):
        answers = iter([[], ["RuvinduH"]])
        self.assertEqual(check("RuvinduH", "User", [], fetch=lambda: next(answers),
                               sleep=lambda s: None), [])

    def test_still_fails_when_never_assigned(self):
        calls = []
        found = check("RuvinduH", "User", [], fetch=lambda: calls.append(1) or [],
                      retries=3, sleep=lambda s: None)
        self.assertEqual(len(found), 1)
        self.assertEqual(len(calls), 3)

    def test_no_fetch_when_the_snapshot_passes(self):
        self.assertEqual(check("RuvinduH", "User", ["RuvinduH"], fetch=lambda: 1 / 0), [])

    def test_without_a_token_it_judges_the_snapshot(self):
        self.assertEqual(len(check("RuvinduH", "User", [], fetch=None)), 1)


if __name__ == "__main__":
    unittest.main()

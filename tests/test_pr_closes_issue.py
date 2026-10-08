"""Tests for scripts/pr_closes_issue.py: a PR closes at least one issue, and only whole issues.
Run: python3 -m unittest discover tests"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from pr_closes_issue import CLOSES, problems, refs  # noqa: E402

REPO = "Akenon-Studio/platform"


class PassesTest(unittest.TestCase):
    def test_closes_an_issue_in_this_repo(self):
        self.assertEqual(problems("Closes #12", REPO), [])

    def test_closes_an_issue_in_another_repo(self):
        self.assertEqual(problems("Closes Akenon-Studio/handbook#67\n\nMore text.", REPO), [])

    def test_every_closing_keyword(self):
        for word in ["close", "closes", "closed", "fix", "fixes", "fixed", "resolve", "resolves",
                     "resolved", "CLOSES", "Fixes:"]:
            self.assertEqual(problems(f"{word} #3", REPO), [], word)

    def test_issue_url(self):
        self.assertEqual(problems("Fixes https://github.com/Akenon-Studio/peras/issues/4", REPO), [])

    def test_part_of_an_issue_it_also_closes(self):
        self.assertEqual(problems("Closes #5. This is part of #5's plan.", REPO), [])

    def test_closing_the_same_issue_by_short_and_long_name(self):
        self.assertEqual(problems("Closes #5, part of akenon-studio/platform#5", REPO), [])


class FailsTest(unittest.TestCase):
    def test_empty_body(self):
        self.assertEqual(len(problems("", REPO)), 1)
        self.assertEqual(len(problems(None, REPO)), 1)

    def test_template_left_blank(self):
        self.assertIn("closes no issue", problems("## What and why\n\nCloses #\n", REPO)[0])

    def test_mention_is_not_closing(self):
        self.assertIn("closes no issue", problems("See #12 and handbook#3", REPO)[0])

    def test_part_of_alone(self):
        found = problems("Part of #24", REPO)
        self.assertEqual(len(found), 2)
        self.assertIn("closes no issue", found[0])
        self.assertIn("akenon-studio/platform#24", found[1])

    def test_closes_one_but_does_part_of_another(self):
        for phrase in ["part of", "Partly", "partially", "towards", "part of issue"]:
            found = problems(f"Closes #5\n\nThis is {phrase} Akenon-Studio/handbook#24.", REPO)
            self.assertEqual(len(found), 1, phrase)
            self.assertIn("akenon-studio/handbook#24", found[0])
            self.assertIn("Split the issue", found[0])

    def test_keyword_inside_an_html_comment_does_not_count(self):
        self.assertIn("closes no issue", problems("<!-- Closes #1 -->", REPO)[0])

    def test_keyword_inside_code_does_not_count(self):
        self.assertIn("closes no issue", problems("Write `Closes #1` in the body", REPO)[0])
        self.assertIn("closes no issue", problems("```\nCloses #1\n```", REPO)[0])

    def test_part_of_inside_code_is_a_quote_not_a_claim(self):
        self.assertEqual(problems("Closes #5. It rejects `Part of #24` bodies.", REPO), [])

    def test_keyword_must_be_a_whole_word(self):
        self.assertIn("closes no issue", problems("prefixes #4", REPO)[0])


class RefsTest(unittest.TestCase):
    def test_refs_are_normalised(self):
        self.assertEqual(refs(CLOSES, "Closes #1, closes Akenon-Studio/Handbook#2", REPO),
                         ["akenon-studio/platform#1", "akenon-studio/handbook#2"])


if __name__ == "__main__":
    unittest.main()

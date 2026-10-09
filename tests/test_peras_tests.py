"""Tests for scripts/peras_tests.py: the issue opened when .github's main breaks peras's tests
(handbook#119). Run: python3 -m unittest discover tests"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from issue_fields import check as form_check, load_forms  # noqa: E402
from one_issue import new_issue  # noqa: E402
from peras_tests import LABEL, TAIL_LINES, TITLE, body  # noqa: E402
from rules import desired_labels  # noqa: E402


class PerasTestsReportTest(unittest.TestCase):
    def test_body_is_a_complete_task_form(self):
        self.assertEqual(form_check("Task", body("FAILED (failures=9)"), load_forms()), [])

    def test_body_shows_the_last_lines_of_the_run(self):
        output = "\n".join(f"line {i}" for i in range(100)) + "\nFAILED (failures=9)"
        text = body(output)
        self.assertIn("FAILED (failures=9)", text)
        self.assertNotIn("line 0\n", text)
        self.assertEqual(text.count("line "), TAIL_LINES - 1)

    def test_issue_carries_its_source_label(self):
        self.assertIn(LABEL, {l["name"] for l in desired_labels(".github")})
        self.assertEqual(new_issue(TITLE, LABEL, body("x"))["labels"], [LABEL])


if __name__ == "__main__":
    unittest.main()

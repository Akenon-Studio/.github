"""Tests for scripts/report_drift.py. Run: python3 -m unittest discover tests"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from issue_fields import check, load_forms  # noqa: E402
from report_drift import body, notes, problems  # noqa: E402

OUTPUT = """note: planning is not managed by rulesets/repos.txt and not archived

FAIL: live settings differ from rulesets/:
  - org: /two_factor_requirement_enabled: expected True, got False
  - board: view 'Audit': Sort by: Severity ascending (now none)
"""


class ReportDriftTest(unittest.TestCase):
    def test_problems_and_notes(self):
        self.assertEqual(problems(OUTPUT), [
            "org: /two_factor_requirement_enabled: expected True, got False",
            "board: view 'Audit': Sort by: Severity ascending (now none)"])
        self.assertEqual(notes(OUTPUT), ["planning is not managed by rulesets/repos.txt and not archived"])

    def test_body_lists_problems_and_notes(self):
        text = body(OUTPUT)
        self.assertIn("- org: /two_factor_requirement_enabled", text)
        self.assertIn("- planning is not managed", text)

    def test_body_is_a_complete_task_form(self):
        # so the issue-fields automation files it on the board without needs-fields
        self.assertEqual(check("Task", body(OUTPUT), load_forms()), [])


if __name__ == "__main__":
    unittest.main()

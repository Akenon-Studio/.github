"""Tests for scripts/report_drift.py. Run: python3 -m unittest discover tests"""

import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from issue_fields import check, load_forms  # noqa: E402
from one_issue import new_issue, update  # noqa: E402
import report_drift  # noqa: E402
from report_drift import LABEL, TITLE, body, notes, problems  # noqa: E402

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


    def test_new_issue_is_assigned_to_the_automation_owners(self):
        made = new_issue(TITLE, LABEL, body(OUTPUT))
        self.assertEqual(made["assignees"], ["RuvinduH"])
        self.assertEqual(made["labels"], ["settings-drift"])
        self.assertEqual(made["type"], "Task")

    def test_update_reassigns_an_unassigned_issue(self):
        text = body(OUTPUT)
        self.assertEqual(update({"body": text, "assignees": []}, text), {"assignees": ["RuvinduH"]})

    def test_update_keeps_whoever_is_assigned(self):
        text = body(OUTPUT)
        self.assertIsNone(update({"body": text, "assignees": [{"login": "Thytus777"}]}, text))
        self.assertEqual(update({"body": "old", "assignees": [{"login": "Thytus777"}]}, text),
                         {"body": text})


class MainTest(unittest.TestCase):
    """The verify code comes in a file: an expression in the workflow's command would print it."""

    def run_main(self, code):
        with tempfile.TemporaryDirectory() as tmp:
            out, code_file = pathlib.Path(tmp, "verify.txt"), pathlib.Path(tmp, "verify.code")
            out.write_text(OUTPUT)
            code_file.write_text(f"{code}\n")
            with mock.patch.object(report_drift, "keep", return_value="kept") as keep, \
                    mock.patch.object(sys, "argv", ["report_drift.py", str(out), str(code_file)]):
                report_drift.main()
            return keep.call_args.args

    def test_drift_opens_the_issue_with_the_problems(self):
        self.assertEqual(self.run_main(1)[3], body(OUTPUT))

    def test_no_drift_closes_it(self):
        self.assertIsNone(self.run_main(0)[3])

    def test_a_broken_check_fails(self):
        with self.assertRaises(SystemExit):
            self.run_main(2)


if __name__ == "__main__":
    unittest.main()

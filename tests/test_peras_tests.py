"""Tests for scripts/peras_tests.py: the issue opened when .github's main breaks peras's tests
(handbook#119). Run: python3 -m unittest discover tests"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from issue_fields import check as form_check, load_forms  # noqa: E402
from one_issue import new_issue  # noqa: E402
from peras_tests import LABEL, REPO, TAIL_LINES, TITLE, body  # noqa: E402
from rules import desired_labels  # noqa: E402

import yaml  # noqa: E402

WORKFLOW = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows" / "peras-tests.yml"


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
        self.assertIn(LABEL, {l["name"] for l in desired_labels(".github-private")})
        self.assertEqual(new_issue(TITLE, LABEL, body("x"))["labels"], [LABEL])


class PerasTestsArePrivateTest(unittest.TestCase):
    """peras is private and .github public: the output goes only to .github-private (design 6.2)."""

    def setUp(self):
        self.workflow = yaml.safe_load(WORKFLOW.read_text())
        self.steps = self.workflow["jobs"]["run"]["steps"]

    def test_the_issue_goes_to_github_private(self):
        self.assertEqual(REPO, "akenon-studio/.github-private")
        self.assertNotIn(LABEL, {l["name"] for l in desired_labels(".github")})

    def test_the_automation_token_reads_only_github_private(self):
        logins = [s["with"] for s in self.steps if "AKENON_APP_ID" in str(s.get("with"))]
        self.assertEqual([w.get("repositories") for w in logins], [".github-private"])

    def test_the_public_log_shows_no_test_output_or_result(self):
        for step in self.steps:
            run = step.get("run", "")
            self.assertNotIn("cat ", run)
            # GitHub prints a step's command with its expressions filled in, and its env: the
            # tests' exit code would show whether they failed
            self.assertNotIn("steps.tests", run + str(step.get("env", "")))
        # The tests' step, whole: their output and code only to files, and green either way
        step = next(s for s in self.steps if "unittest" in s.get("run", ""))
        self.assertEqual(set(step), {"name", "env", "run"})  # no tracing shell, no continue-on-error
        self.assertNotIn("defaults", self.workflow)  # nor one for every step
        self.assertNotIn("defaults", self.workflow["jobs"]["run"])
        self.assertEqual(set(step["env"]), {"PERAS_DOT_GITHUB"})
        tests = step["run"]
        self.assertEqual([l for l in tests.splitlines() if l.strip() and not l.strip().startswith("#")], [
            "set +e",
            "(cd peras && python3 -m unittest discover -s core/workspace/tests) > tests.txt 2>&1",
            'echo "$?" > tests.code',
            'echo "Ran; the result is in .github-private"'])
        report = next(s["run"] for s in self.steps if "peras_tests.py" in s.get("run", ""))
        self.assertTrue(report.startswith("python3 scripts/quiet_run.py "))


if __name__ == "__main__":
    unittest.main()

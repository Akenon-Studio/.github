"""Tests for scripts/quiet_run.py and where the weakness reports go (design 6.2)."""

import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import quiet_run  # noqa: E402
import report_drift  # noqa: E402
import stale_alerts  # noqa: E402


def run(code):
    """quiet_run.py on a script holding `code`, named as the workflow's steps are."""
    with tempfile.TemporaryDirectory() as d:
        script = pathlib.Path(d) / "step.py"
        script.write_text(code)
        return subprocess.run([sys.executable, str(ROOT / "scripts" / "quiet_run.py"), sys.executable,
                               str(script), "collect"], capture_output=True, text=True)


class QuietRunTest(unittest.TestCase):
    def test_a_success_prints_only_that_it_passed(self):
        r = run("print('3 open alert(s)')")
        self.assertEqual(r.returncode, 0)
        self.assertIn("collect passed", r.stdout)
        self.assertNotIn("alert", r.stdout)

    def test_a_crash_prints_only_the_exception_type(self):
        r = run("raise KeyError('platform: required check missing')")
        self.assertEqual(r.returncode, 1)
        self.assertIn("KeyError", r.stdout)
        self.assertIn("collect", r.stdout)  # names the step
        self.assertNotIn("platform", r.stdout + r.stderr)

    def test_an_exit_message_is_not_printed(self):
        r = run("import sys; sys.exit('alerts off in platform')")
        self.assertEqual(r.returncode, 1)
        self.assertIn("unknown", r.stdout)
        self.assertNotIn("platform", r.stdout + r.stderr)

    def test_error_type_finds_the_last_exception(self):
        self.assertEqual(quiet_run.error_type("Traceback\nValueError: x\njson.decoder.JSONDecodeError: y"),
                         "json.decoder.JSONDecodeError")
        self.assertEqual(quiet_run.error_type("gh: Not Found (HTTP 404)"), "unknown")


class VerdictTest(unittest.TestCase):
    def test_pass_drift_and_crash(self):
        passed = quiet_run.verdict(0, "PASS: ...")
        drift = quiet_run.verdict(1, "\nFAIL: live settings differ from rulesets/:\n  - org: 2FA off")
        self.assertEqual((passed[0], drift[0]), (0, 1))
        # The public log can't tell drift from a pass: that would itself show a weakness
        self.assertEqual(passed[1], drift[1])
        # A Python crash also exits 1, but has no FAIL line
        code, line = quiet_run.verdict(1, "Traceback (most recent call last):\nKeyError: 'platform'")
        self.assertEqual((code, line), (2, "verify-settings.py crashed: KeyError"))
        self.assertEqual(quiet_run.verdict(2, "gh: Not Found")[0], 2)


class ReportsArePrivateTest(unittest.TestCase):
    def test_the_weakness_reports_go_to_github_private(self):
        self.assertEqual(report_drift.REPO, "akenon-studio/.github-private")
        self.assertEqual(stale_alerts.REPO, "akenon-studio/.github-private")

    def test_collect_refuses_a_partial_list(self):
        saved = stale_alerts.managed_repos, stale_alerts.try_gh
        stale_alerts.managed_repos = lambda: ["platform"]
        stale_alerts.try_gh = lambda *a: None
        try:
            with self.assertRaises(SystemExit) as e:
                stale_alerts.collect("/dev/null")
        finally:
            stale_alerts.managed_repos, stale_alerts.try_gh = saved
        self.assertIn("platform", str(e.exception.code))  # named for an owner running it locally

    def test_quiet_run_hides_the_unreadable_repos_names(self):
        r = run("import sys; sys.exit('Could not read Dependabot alerts in platform (alerts off)')")
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("platform", r.stdout + r.stderr)

if __name__ == "__main__":
    unittest.main()

"""Tests for scripts/quiet_run.py and where the weakness reports go (design 6.2)."""

import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import quiet_run  # noqa: E402
import report_drift  # noqa: E402
import stale_alerts  # noqa: E402


def run(*code):
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "quiet_run.py"), sys.executable, "-c", *code],
                          capture_output=True, text=True)


class QuietRunTest(unittest.TestCase):
    def test_a_success_prints_its_output(self):
        r = run("print('3 open alert(s)')")
        self.assertEqual((r.returncode, r.stdout), (0, "3 open alert(s)\n"))

    def test_a_crash_prints_only_the_exception_type(self):
        r = run("raise KeyError('platform: required check missing')")
        self.assertEqual(r.returncode, 1)
        self.assertIn("KeyError", r.stdout)
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


class ReportsArePrivateTest(unittest.TestCase):
    def test_the_weakness_reports_go_to_github_private(self):
        self.assertEqual(report_drift.REPO, "akenon-studio/.github-private")
        self.assertEqual(stale_alerts.REPO, "akenon-studio/.github-private")

    def test_unreadable_repos_are_counted_not_named(self):
        saved = stale_alerts.managed_repos, stale_alerts.try_gh
        stale_alerts.managed_repos = lambda: ["platform"]
        stale_alerts.try_gh = lambda *a: None
        try:
            with self.assertRaises(SystemExit) as e:
                stale_alerts.collect("/dev/null")
        finally:
            stale_alerts.managed_repos, stale_alerts.try_gh = saved
        self.assertIn("1 repo(s)", str(e.exception.code))
        self.assertNotIn("platform", str(e.exception.code))


if __name__ == "__main__":
    unittest.main()

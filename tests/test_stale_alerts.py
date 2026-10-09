"""Tests for scripts/stale_alerts.py (design 6.4). Run: python3 -m unittest discover tests"""

import datetime
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
from issue_fields import check, load_forms  # noqa: E402
import stale_alerts  # noqa: E402
from stale_alerts import body, fix_pr, plain, stale  # noqa: E402

NOW = datetime.datetime(2026, 10, 20, tzinfo=datetime.timezone.utc)


def alert(created, fixed=None):
    return {"repo": "platform", "package": "braces", "severity": "high", "summary": "ReDoS",
            "created_at": created, "url": "https://github.com/x", "fixed_in": fixed}


class StaleTest(unittest.TestCase):
    def test_only_alerts_older_than_three_days(self):
        old, new = alert("2026-10-16T00:00:00Z"), alert("2026-10-18T00:00:00Z")
        self.assertEqual(stale([old, new], NOW), [old])

    def test_body_is_a_valid_task_form(self):
        text = body([alert("2026-10-01T00:00:00Z"), alert("2026-10-01T00:00:00Z", "3.0.3")])
        self.assertEqual(check("Task", text, load_forms()), [])
        self.assertIn("no fixed version yet", text)
        self.assertIn("fixed in 3.0.3", text)


class FixPrTest(unittest.TestCase):
    def setUp(self):
        self.calls, self.real = [], stale_alerts.try_gh

    def tearDown(self):
        stale_alerts.try_gh = self.real

    def fake(self, result):
        def try_gh(*args):
            self.calls.append(args)
            return result
        stale_alerts.try_gh = try_gh

    def test_one_read_per_repo_and_whole_word_titles(self):
        self.fake([{"title": "fix(deps): update dependency braces to v3.0.4",
                    "user": {"login": "renovate[bot]"}},
                   {"title": "fix: braces by hand", "user": {"login": "someone"}}])
        cache = {}
        self.assertTrue(fix_pr(alert("2026-10-01T00:00:00Z"), cache))
        self.assertFalse(fix_pr(dict(alert("2026-10-01T00:00:00Z"), package="brace"), cache))
        self.assertEqual(len(self.calls), 1)

    def test_a_persons_pr_does_not_count(self):
        self.fake([{"title": "fix: update braces", "user": {"login": "someone"}}])
        self.assertFalse(fix_pr(alert("2026-10-01T00:00:00Z"), {}))

    def test_unreadable_prs_are_unknown_not_no_pr(self):
        self.fake(None)
        self.assertIsNone(fix_pr(alert("2026-10-01T00:00:00Z"), {}))


class PlainTest(unittest.TestCase):
    def test_advisory_text_cannot_mention_or_link(self):
        self.assertEqual(plain("ping @someone `x`\n[y](z)"), "`ping @someone 'x' [y](z)`")


if __name__ == "__main__":
    unittest.main()

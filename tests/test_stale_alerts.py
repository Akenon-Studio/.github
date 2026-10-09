"""Tests for scripts/stale_alerts.py (design 6.4). Run: python3 -m unittest discover tests"""

import datetime
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
from issue_fields import check, load_forms  # noqa: E402
from stale_alerts import body, stale  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()

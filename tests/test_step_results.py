"""Tests for scripts/step_results.py. Run: python3 -m unittest discover tests"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from step_results import failed, parse  # noqa: E402


class StepResultsTest(unittest.TestCase):
    def test_parse_keeps_the_order(self):
        self.assertEqual(parse("title=success docs=failure"), [("title", "success"), ("docs", "failure")])

    def test_all_passing(self):
        self.assertEqual(failed(parse("a=success b=success"), set()), ([], []))

    def test_an_advisory_failure_is_only_a_warning(self):
        self.assertEqual(failed(parse("a=success b=failure"), {"b"}), ([], ["b"]))

    def test_a_step_that_did_not_finish_fails(self):
        # skipped after a setup step failed, or an empty outcome: neither is a pass
        self.assertEqual(failed(parse("a=skipped b=cancelled c="), set()), (["a", "b", "c"], []))


if __name__ == "__main__":
    unittest.main()

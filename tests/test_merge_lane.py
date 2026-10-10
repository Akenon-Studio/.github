"""Tests for scripts/merge_lane.py (design 6.2). Run: python3 -m unittest discover tests"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from merge_lane import NEVER_RAN, check_states, decide, queue  # noqa: E402

REQUIRED = {"pr-title / checks", "security-scan / code"}


def run(name, status="COMPLETED", conclusion="SUCCESS", required=True):
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion,
            "isRequired": required}


def pr(contexts=None, state="OPEN", draft=False, mergeable="MERGEABLE", status="CLEAN",
       labels=("ready-to-merge",), threads=(), review=None):
    if contexts is None:
        contexts = [run("pr-title / checks"), run("security-scan / code")]
    return {"number": 7, "state": state, "isDraft": draft, "mergeable": mergeable,
            "mergeStateStatus": status, "reviewDecision": review, "headRefOid": "abc",
            "author": {"login": "someone"}, "labels": {"nodes": [{"name": n} for n in labels]},
            "reviewThreads": {"nodes": [{"isResolved": r} for r in threads]},
            "commits": {"nodes": [{"commit": {"oid": "abc", "statusCheckRollup": {
                "contexts": {"nodes": contexts}}}}]}}


class DecideTest(unittest.TestCase):
    def action(self, p, missing_for=0):
        return decide(p, REQUIRED, missing_for)[0]

    def test_green_and_clean_merges(self):
        self.assertEqual(self.action(pr()), "merge")

    def test_a_failed_check_that_is_not_required_still_merges(self):
        p = pr(contexts=[run("pr-title / checks"), run("security-scan / code"),
                         run("ai-review / review", conclusion="FAILURE", required=False)], status="UNSTABLE")
        self.assertEqual(self.action(p), "merge")

    def test_behind_brings_main_in(self):
        self.assertEqual(self.action(pr(status="BEHIND")), "update")

    def test_running_checks_wait(self):
        p = pr(contexts=[run("pr-title / checks", status="IN_PROGRESS", conclusion=None),
                         run("security-scan / code")], status="BLOCKED")
        self.assertEqual(self.action(p), "wait")

    def test_a_failed_required_check_leaves_the_lane(self):
        p = pr(contexts=[run("pr-title / checks", conclusion="FAILURE"), run("security-scan / code")],
               status="BLOCKED")
        action, reason = decide(p, REQUIRED, 0)
        self.assertEqual(action, "drop")
        self.assertIn("pr-title / checks", reason)

    def test_a_rerun_that_passed_wins_over_the_old_failure(self):
        p = pr(contexts=[run("pr-title / checks", conclusion="FAILURE"), run("pr-title / checks"),
                         run("security-scan / code")])
        self.assertEqual(self.action(p), "merge")

    def test_a_missing_check_waits_then_leaves(self):
        p = pr(contexts=[run("pr-title / checks")], status="BLOCKED")
        self.assertEqual(self.action(p, missing_for=30), "wait")
        action, reason = decide(p, REQUIRED, NEVER_RAN + 1)
        self.assertEqual(action, "drop")
        self.assertIn("security-scan / code", reason)

    def test_conflicts_drafts_and_unlabelled(self):
        self.assertEqual(self.action(pr(mergeable="CONFLICTING", status="DIRTY")), "drop")
        self.assertEqual(self.action(pr(draft=True)), "drop")
        self.assertEqual(self.action(pr(labels=())), "gone")
        self.assertEqual(self.action(pr(state="MERGED")), "gone")

    def test_green_but_blocked_says_why(self):
        action, reason = decide(pr(status="BLOCKED", threads=(False, True), review="REVIEW_REQUIRED"),
                                REQUIRED, 0)
        self.assertEqual(action, "drop")
        self.assertIn("1 unresolved conversation", reason)
        self.assertIn("approval", reason)

    def test_unknown_waits(self):
        self.assertEqual(self.action(pr(mergeable="UNKNOWN", status="UNKNOWN")), "wait")


class QueueTest(unittest.TestCase):
    def test_oldest_label_first_by_its_latest_labelling(self):
        def node(n, *events):
            return {"number": n, "timelineItems": {"nodes": [
                {"createdAt": t, "label": {"name": name}} for name, t in events]}}
        data = {"pullRequests": {"nodes": [
            node(3, ("ready-to-merge", "2026-10-10T03:00:00Z")),
            # labelled early, taken out, labelled again later: it goes to the back
            node(1, ("ready-to-merge", "2026-10-10T01:00:00Z"), ("ready-to-merge", "2026-10-10T04:00:00Z")),
            node(2, ("task", "2026-10-10T00:00:00Z"), ("ready-to-merge", "2026-10-10T02:00:00Z")),
        ]}}
        self.assertEqual(queue(data), [2, 3, 1])


class CheckStatesTest(unittest.TestCase):
    def test_commit_statuses_count_too(self):
        p = pr(contexts=[run("pr-title / checks"),
                         {"__typename": "StatusContext", "context": "security-scan / code", "state": "PENDING",
                          "isRequired": True}])
        self.assertEqual(check_states(p, REQUIRED)["security-scan / code"], "PENDING")


if __name__ == "__main__":
    unittest.main()

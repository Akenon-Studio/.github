"""Tests for scripts/merge_lane.py (design 6.2). Run: python3 -m unittest discover tests"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

import urllib.error  # noqa: E402
from unittest import mock  # noqa: E402

import merge_lane  # noqa: E402
from merge_lane import BLOCKED_LOOKS, NEVER_RAN, check_states, decide, queue, run_one  # noqa: E402

REQUIRED = {"pr-title / checks", "security-scan / code"}


def run(name, status="COMPLETED", conclusion="SUCCESS", required=True, started="2026-10-10T01:00:00Z"):
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion,
            "isRequired": required, "startedAt": started}


def pr(contexts=None, state="OPEN", draft=False, mergeable="MERGEABLE", status="CLEAN",
       labels=("ready-to-merge",), threads=(), review=None, head="abc"):
    if contexts is None:
        contexts = [run("pr-title / checks"), run("security-scan / code")]
    return {"number": 7, "state": state, "isDraft": draft, "mergeable": mergeable,
            "mergeStateStatus": status, "reviewDecision": review, "headRefOid": head, "baseRefName": "main",
            "author": {"login": "someone"}, "labels": {"nodes": [{"name": n} for n in labels]},
            "reviewThreads": {"nodes": [{"isResolved": r} for r in threads]},
            "commits": {"nodes": [{"commit": {"oid": head, "statusCheckRollup": {
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

    def test_the_newest_run_of_a_check_counts_in_any_order(self):
        old_fail = run("pr-title / checks", conclusion="FAILURE", started="2026-10-10T01:00:00Z")
        new_pass = run("pr-title / checks", started="2026-10-10T02:00:00Z")
        for contexts in ([old_fail, new_pass], [new_pass, old_fail]):
            self.assertEqual(self.action(pr(contexts=contexts + [run("security-scan / code")])), "merge")
        old_pass = run("pr-title / checks", started="2026-10-10T01:00:00Z")
        new_fail = run("pr-title / checks", conclusion="FAILURE", started="2026-10-10T02:00:00Z")
        for contexts in ([old_pass, new_fail], [new_fail, old_pass]):
            self.assertEqual(self.action(pr(contexts=contexts + [run("security-scan / code")],
                                            status="BLOCKED")), "drop")

    def test_a_missing_check_waits_then_leaves(self):
        p = pr(contexts=[run("pr-title / checks")], status="BLOCKED")
        self.assertEqual(self.action(p, missing_for=30), "wait")
        action, reason = decide(p, REQUIRED, NEVER_RAN + 1)
        self.assertEqual(action, "drop")
        self.assertIn("security-scan / code", reason)

    def test_only_prs_into_main(self):
        p = pr()
        p["baseRefName"] = "feat/12-other"
        self.assertEqual(self.action(p), "drop")

    def test_conflicts_drafts_and_unlabelled(self):
        self.assertEqual(self.action(pr(mergeable="CONFLICTING", status="DIRTY")), "drop")
        self.assertEqual(self.action(pr(draft=True)), "drop")
        self.assertEqual(self.action(pr(labels=())), "gone")
        self.assertEqual(self.action(pr(state="MERGED")), "gone")

    def test_green_but_blocked_says_why(self):
        # "blocked", not "drop": GitHub's merge state lags the checks by a few seconds, so the lane
        # drops it only once it stays blocked (BLOCKED_LOOKS)
        action, reason = decide(pr(status="BLOCKED", threads=(False, True), review="REVIEW_REQUIRED"),
                                REQUIRED, 0)
        self.assertEqual(action, "blocked")
        self.assertIn("1 unresolved conversation", reason)
        self.assertIn("approval", reason)

    def test_more_checks_than_it_reads_leaves_the_lane(self):
        p = pr()
        p["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["pageInfo"] = {"hasNextPage": True}
        self.assertEqual(self.action(p), "drop")

    def test_open_conversations_count_only_where_the_rules_say(self):
        p = pr(threads=(False,))
        self.assertEqual(decide(p, REQUIRED, 0, threads_required=True)[0], "blocked")
        self.assertEqual(decide(p, REQUIRED, 0, threads_required=False)[0], "merge")

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

    def test_a_pr_whose_labelling_is_too_old_to_see_goes_to_the_back(self):
        data = {"pullRequests": {"nodes": [
            {"number": 5, "timelineItems": {"nodes": []}},
            {"number": 6, "timelineItems": {"nodes": [{"createdAt": "2026-10-10T01:00:00Z",
                                                        "label": {"name": "ready-to-merge"}}]}}]}}
        self.assertEqual(queue(data), [6, 5])


class CheckStatesTest(unittest.TestCase):
    def test_a_queued_rerun_is_newer_than_an_old_failure(self):
        old_fail = run("pr-title / checks", conclusion="FAILURE", started="2026-10-10T01:00:00Z")
        queued = run("pr-title / checks", status="QUEUED", conclusion=None, started=None)
        for contexts in ([old_fail, queued], [queued, old_fail]):
            self.assertEqual(check_states(pr(contexts=contexts), REQUIRED)["pr-title / checks"], "PENDING")

    def test_commit_statuses_count_too(self):
        p = pr(contexts=[run("pr-title / checks"),
                         {"__typename": "StatusContext", "context": "security-scan / code", "state": "PENDING",
                          "isRequired": True}])
        self.assertEqual(check_states(p, REQUIRED)["security-scan / code"], "PENDING")


class FakeGitHub:
    """Serves a list of PR states, one per look, and records every write."""
    repo = "Akenon-Studio/x"

    def __init__(self, looks, fail=None):
        self.looks, self.calls, self.fail = list(looks), [], fail or {}

    def graphql(self, query, **variables):
        return {"pullRequest": self.looks.pop(0) if len(self.looks) > 1 else self.looks[0]}

    def call(self, path, method="GET", body=None, tries=4):
        self.calls.append((method, path))
        code = self.fail.get(path.rsplit("/", 1)[-1])
        if code:
            raise urllib.error.HTTPError(path, code, "no", {}, None)


class Clock:
    """Fake time: sleeping moves it on, so a wait that never ends shows up as a timeout drop."""
    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class RunOneTest(unittest.TestCase):
    def setUp(self):
        clock = Clock()
        for name in ("time", "sleep"):
            patcher = mock.patch.object(merge_lane.time, name, getattr(clock, name))
            patcher.start()
            self.addCleanup(patcher.stop)

    def writes(self, gh):
        return [(m, p.split("/", 4)[-1]) for m, p in gh.calls]

    def test_says_why_before_taking_the_label_off(self):
        gh = FakeGitHub([pr(draft=True)])
        self.assertEqual(run_one(gh, 7, REQUIRED, float("inf")), "done")
        self.assertEqual(self.writes(gh), [("POST", "7/comments"), ("DELETE", "7/labels/ready-to-merge")])

    def test_a_blocked_look_or_two_is_waited_out(self):
        blocked = pr(status="BLOCKED")
        gh = FakeGitHub([blocked, blocked, pr()])
        run_one(gh, 7, REQUIRED, float("inf"))
        self.assertEqual(self.writes(gh), [("PUT", "7/merge")])

    def test_staying_blocked_leaves_the_lane(self):
        gh = FakeGitHub([pr(status="BLOCKED")] * BLOCKED_LOOKS)
        run_one(gh, 7, REQUIRED, float("inf"))
        self.assertEqual(self.writes(gh)[-1], ("DELETE", "7/labels/ready-to-merge"))

    def test_brings_main_in_then_merges(self):
        gh = FakeGitHub([pr(status="BEHIND"), pr(head="def")])
        run_one(gh, 7, REQUIRED, float("inf"))
        self.assertEqual(self.writes(gh), [("PUT", "7/update-branch"), ("PUT", "7/merge")])

    def test_a_lagging_behind_after_the_update_is_waited_out(self):
        # GitHub still shows the old commit, still BEHIND, for a few looks: no second update
        gh = FakeGitHub([pr(status="BEHIND"), pr(status="BEHIND"), pr(status="BEHIND"), pr(head="def")])
        run_one(gh, 7, REQUIRED, float("inf"))
        self.assertEqual(self.writes(gh), [("PUT", "7/update-branch"), ("PUT", "7/merge")])

    def test_an_update_that_never_shows_leaves_the_lane(self):
        gh = FakeGitHub([pr(status="BEHIND")])
        run_one(gh, 7, REQUIRED, float("inf"))
        self.assertEqual(self.writes(gh)[0], ("PUT", "7/update-branch"))
        self.assertEqual(self.writes(gh)[-1], ("DELETE", "7/labels/ready-to-merge"))
        self.assertEqual(sum(w == ("PUT", "7/update-branch") for w in self.writes(gh)), 1)

    def test_an_update_error_is_retried_then_drops(self):
        gh = FakeGitHub([pr(status="BEHIND")], fail={"update-branch": 422})
        run_one(gh, 7, REQUIRED, float("inf"))
        self.assertEqual([w for w in self.writes(gh) if w[1] == "7/update-branch"], [("PUT", "7/update-branch")] * 3)
        self.assertEqual(self.writes(gh)[-1], ("DELETE", "7/labels/ready-to-merge"))

    def test_an_outage_while_bringing_main_in_is_not_the_prs_fault(self):
        gh = FakeGitHub([pr(status="BEHIND")], fail={"update-branch": 502})
        with self.assertRaises(urllib.error.HTTPError):
            run_one(gh, 7, REQUIRED, float("inf"))
        self.assertNotIn(("DELETE", "7/labels/ready-to-merge"), self.writes(gh))

    def test_update_errors_count_only_in_a_row(self):
        # fail, succeed (new commit, still behind), fail, succeed: never 3 failures in a row
        looks = [pr(status="BEHIND"), pr(status="BEHIND"), pr(status="BEHIND", head="b"),
                 pr(status="BEHIND", head="b"), pr(head="c")]
        gh = FakeGitHub(looks)
        calls, real = {"n": 0}, gh.call

        def flaky(path, method="GET", body=None, tries=4):
            if path.endswith("update-branch"):
                calls["n"] += 1
                if calls["n"] % 2:
                    gh.calls.append((method, path))
                    raise urllib.error.HTTPError(path, 422, "moved", {}, None)
            return real(path, method, body, tries)
        gh.call = flaky
        run_one(gh, 7, REQUIRED, float("inf"))
        self.assertEqual(self.writes(gh)[-1], ("PUT", "7/merge"))

    def test_main_moving_again_and_again_drops(self):
        gh = FakeGitHub([pr(status="BEHIND", head=h) for h in "abcd"])
        run_one(gh, 7, REQUIRED, float("inf"))
        self.assertEqual(sum(w == ("PUT", "7/update-branch") for w in self.writes(gh)), 3)
        self.assertEqual(self.writes(gh)[-1], ("DELETE", "7/labels/ready-to-merge"))

    def test_the_same_reason_is_not_said_twice(self):
        first = FakeGitHub([pr(draft=True)])
        run_one(first, 7, REQUIRED, float("inf"))
        said = "<!-- merge-lane -->\n@someone Taken out of the merge lane: it is a draft. " \
               "Label it `ready-to-merge` again when it's ready (design 6.2)."
        labelled = {"__typename": "LabeledEvent", "label": {"name": "ready-to-merge"}}
        comment = {"__typename": "IssueComment", "body": said}
        # the comment went up but the label couldn't come off: only the label is taken off now
        again = pr(draft=True)
        again["timelineItems"] = {"nodes": [labelled, comment]}
        gh = FakeGitHub([again])
        run_one(gh, 7, REQUIRED, float("inf"))
        self.assertEqual(self.writes(gh), [("DELETE", "7/labels/ready-to-merge")])
        # labelled again without fixing it: the author is told again
        relabelled = pr(draft=True)
        relabelled["timelineItems"] = {"nodes": [labelled, comment, labelled]}
        gh = FakeGitHub([relabelled])
        run_one(gh, 7, REQUIRED, float("inf"))
        self.assertEqual(self.writes(gh)[0], ("POST", "7/comments"))

    def test_a_locked_conversation_still_takes_the_label_off(self):
        gh = FakeGitHub([pr(draft=True)], fail={"comments": 403})
        run_one(gh, 7, REQUIRED, float("inf"))
        self.assertEqual(self.writes(gh)[-1], ("DELETE", "7/labels/ready-to-merge"))

    def test_a_rate_limit_while_commenting_stops_the_run(self):
        gh = FakeGitHub([pr(draft=True)])
        real = gh.call

        def limited(path, method="GET", body=None, tries=4):
            if path.endswith("comments"):
                raise urllib.error.HTTPError(path, 403, "rate limit", {"x-ratelimit-remaining": "0"}, None)
            return real(path, method, body, tries)
        gh.call = limited
        with self.assertRaises(urllib.error.HTTPError):
            run_one(gh, 7, REQUIRED, float("inf"))
        self.assertNotIn(("DELETE", "7/labels/ready-to-merge"), self.writes(gh))

    def test_a_failed_check_leaves_before_main_is_brought_in(self):
        gh = FakeGitHub([pr(contexts=[run("pr-title / checks", conclusion="FAILURE"), run("security-scan / code")],
                            status="BEHIND")])
        run_one(gh, 7, REQUIRED, float("inf"))
        self.assertNotIn(("PUT", "7/update-branch"), self.writes(gh))

    def test_a_missing_approval_leaves_before_main_is_brought_in(self):
        gh = FakeGitHub([pr(status="BEHIND", review="REVIEW_REQUIRED")])
        run_one(gh, 7, REQUIRED, float("inf"))
        self.assertNotIn(("PUT", "7/update-branch"), self.writes(gh))
        self.assertEqual(self.writes(gh)[-1], ("DELETE", "7/labels/ready-to-merge"))

    def test_repeated_merge_refusals_leave_the_lane(self):
        gh = FakeGitHub([pr()], fail={"merge": 405})
        run_one(gh, 7, REQUIRED, float("inf"))
        self.assertEqual(sum(w == ("PUT", "7/merge") for w in self.writes(gh)), 3)
        self.assertEqual(self.writes(gh)[-1], ("DELETE", "7/labels/ready-to-merge"))

    def test_the_deadline_pauses_without_dropping(self):
        running = pr(contexts=[run("pr-title / checks", status="IN_PROGRESS", conclusion=None),
                               run("security-scan / code")], status="BLOCKED")
        gh = FakeGitHub([running])
        self.assertEqual(run_one(gh, 7, REQUIRED, 0), "paused")
        self.assertEqual(self.writes(gh), [])

class MainTest(unittest.TestCase):
    def test_an_outage_stops_the_run_and_leaves_every_label_on(self):
        class Down(FakeGitHub):
            def main_rules(self):
                return REQUIRED, True

            def graphql(self, query, **variables):
                if "pullRequests(" in query:  # the queue reads fine; then GitHub goes down
                    return {"pullRequests": {"nodes": [{"number": 7, "timelineItems": {"nodes": []}}]}}
                raise urllib.error.HTTPError("graphql", 502, "bad gateway", {}, None)
        gh = Down([pr()])
        with mock.patch.object(merge_lane, "GitHub", lambda token, repo: gh), \
                mock.patch.dict("os.environ", {"GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": "Akenon-Studio/x"}):
            with self.assertRaises(urllib.error.HTTPError):
                merge_lane.main()
        self.assertEqual(gh.calls, [])  # no comment, no label taken off

if __name__ == "__main__":
    unittest.main()

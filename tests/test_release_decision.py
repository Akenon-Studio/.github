"""Tests for scripts/release_decision.py: a Release PR merges only on a partner's own comment newer
than the push that made its head commit its head (design 6.3). Run: python3 -m unittest discover tests"""

import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

import release_decision  # noqa: E402
from release_decision import decision, head_pushed_at, is_release_pr, problems  # noqa: E402

PEOPLE = {"ruvinduh", "thytus777"}
BRANCH = "release-please--branches--main"
PUSHED = "2026-11-02T10:00:00Z"


def comment(login="RuvinduH", body="Cut it.", at="2026-11-02T11:00:00Z", kind="User"):
    return {"author": {"__typename": kind, "login": login}, "body": body, "createdAt": at,
            "url": f"https://github.com/x/y/pull/9#c-{login}-{at}"}


def pr(author="akenon-studio-release", kind="Bot", branch=BRANCH, labels=(), ever=(), comments=(),
       state="OPEN", fork=False, sha="a" * 40):
    return {"number": 9, "url": "https://github.com/x/y/pull/9", "state": state, "isCrossRepository": fork,
            "headRefName": branch, "headRefOid": sha,
            "author": {"__typename": kind, "login": author},
            "labels": {"nodes": [{"name": l} for l in labels]},
            "timelineItems": {"nodes": [{"label": {"name": l}} for l in ever]},
            "comments": {"nodes": list(comments)}}


def ordinary(**kw):
    return pr(author="RuvinduH", kind="User", branch="feat/1-x", **kw)


def push(after="a" * 40, at=PUSHED, kind="push", ref=f"refs/heads/{BRANCH}"):
    return {"activity_type": kind, "after": after, "timestamp": at, "ref": ref}


class WhatIsAReleasePrTest(unittest.TestCase):
    def test_the_release_apps_pr(self):
        self.assertTrue(is_release_pr(pr(branch="other")))
        self.assertTrue(is_release_pr(pr(author="akenon-studio-release[bot]", branch="other")))

    def test_a_person_named_like_the_app_is_not_it(self):
        self.assertFalse(is_release_pr(pr(kind="User", branch="other")))

    def test_a_release_please_branch(self):
        self.assertTrue(is_release_pr(ordinary() | {"headRefName": BRANCH}))

    def test_the_label_now_or_ever(self):
        self.assertTrue(is_release_pr(ordinary(labels=["autorelease: pending"])))
        # taking the label off doesn't make it an ordinary PR
        self.assertTrue(is_release_pr(ordinary(ever=["autorelease: pending"])))
        self.assertFalse(is_release_pr(ordinary(labels=["autorelease: tagged"], ever=["release"])))

    def test_any_other_pr(self):
        self.assertFalse(is_release_pr(ordinary()))
        self.assertFalse(is_release_pr(pr(author="renovate", branch="renovate/x")))


class PushTimeTest(unittest.TestCase):
    """The clock is the server's record of the push, never a commit's dates (design 6.3)."""

    def test_the_latest_push_that_made_the_commit_the_head(self):
        activity = [push(at="2026-11-02T09:00:00Z", kind="branch_creation"), push(at="2026-11-02T12:00:00Z", kind="force_push"),
                    push(after="b" * 40, at="2026-11-02T13:00:00Z")]
        self.assertEqual(head_pushed_at(activity, BRANCH, "a" * 40), "2026-11-02T12:00:00Z")

    def test_a_push_elsewhere_or_of_another_commit_doesnt_count(self):
        # GitHub may have seen the commit first on another branch, before the comment
        self.assertIsNone(head_pushed_at([push(ref="refs/heads/other"), push(after="b" * 40),
                                          push(kind="branch_deletion")], BRANCH, "a" * 40))


class DecisionTest(unittest.TestCase):
    def test_a_partners_own_comment_after_the_push_passes(self):
        self.assertEqual(problems(pr(comments=[comment()]), PEOPLE, PUSHED), [])
        self.assertEqual(problems(pr(comments=[comment(login="thytus777")]), PEOPLE, PUSHED), [])
        self.assertEqual(problems(pr(comments=[comment(at=PUSHED)]), PEOPLE, PUSHED), [])  # the same second

    def test_no_comment_fails(self):
        found = problems(pr(), PEOPLE, PUSHED)
        self.assertEqual(len(found), 1)
        self.assertIn("#9", found[0])
        self.assertIn("design 6.3", found[0])

    def test_a_comment_older_than_the_push_fails(self):
        # every push changes what would ship, so it needs a new comment
        self.assertEqual(len(problems(pr(comments=[comment(at="2026-11-02T09:59:59Z")]), PEOPLE, PUSHED)), 1)

    def test_no_recorded_push_fails_whatever_the_comments(self):
        found = problems(pr(fork=True, comments=[comment()]), PEOPLE, None)
        self.assertEqual(len(found), 1)
        self.assertIn("no push", found[0])
        self.assertIsNone(decision(pr(comments=[comment()]), PEOPLE, None))

    def test_an_agents_comment_never_counts(self):
        for body in ("<!-- agent -->\nCut it.", "Cut it. <!--agent-->", "<!-- AGENT -->"):
            self.assertEqual(len(problems(pr(comments=[comment(body=body)]), PEOPLE, PUSHED)), 1, body)

    def test_only_a_person_in_the_teams_counts(self):
        self.assertEqual(len(problems(pr(comments=[comment(login="outsider")]), PEOPLE, PUSHED)), 1)
        # bots never count, even one with a partner's name
        self.assertEqual(len(problems(pr(comments=[comment(login="ruvinduh", kind="Bot")]), PEOPLE, PUSHED)), 1)
        deleted = {"author": None, "body": "x", "createdAt": "2026-11-02T12:00:00Z"}
        self.assertEqual(len(problems(pr(comments=[deleted]), PEOPLE, PUSHED)), 1)

    def test_the_newest_counting_comment_is_the_decision(self):
        older, newer = comment(at="2026-11-02T11:00:00Z"), comment(login="Thytus777", at="2026-11-02T12:00:00Z")
        self.assertIs(decision(pr(comments=[older, newer, comment(body="<!-- agent -->")]), PEOPLE, PUSHED), newer)

    def test_every_other_pr_passes(self):
        self.assertEqual(problems(ordinary(), PEOPLE, None), [])
        self.assertEqual(problems(pr(author="renovate", branch="renovate/x"), PEOPLE, None), [])

    def test_the_partners_come_from_the_teams_file(self):
        self.assertTrue(release_decision.org_people())  # rules.org_people(), as issue_fields.py reads them


class EventsTest(unittest.TestCase):
    """main(): which PR each event judges, and what a comment does."""

    def run_main(self, event_name, event, the_pr, activity=(), runs=(), polls=None):
        posted, slept = [], []
        polls = iter(polls or [])

        def gh(path, *args, **kw):
            if args == ("-X", "POST"):
                posted.append(path)
                return None
            if "/activity?" in path:
                assert f"ref=refs/heads/{the_pr['headRefName']}" in path, path
                return list(activity)
            if "/actions/runs?" in path:
                assert f"head_sha={the_pr['headRefOid']}" in path, path
                return {"workflow_runs": list(runs)}
            return next(polls)

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(event, f)
        environ = {"GITHUB_EVENT_NAME": event_name, "GITHUB_REPOSITORY": "akenon-studio/peras"}
        code = 0
        with mock.patch.object(release_decision, "graphql",
                               lambda q, **v: {"repository": {"pullRequest": the_pr}}), \
                mock.patch.object(release_decision, "gh", gh), \
                mock.patch.object(release_decision, "org_people", lambda: PEOPLE), \
                mock.patch.object(release_decision.time, "sleep", slept.append), \
                mock.patch.dict(os.environ, environ), mock.patch.object(sys, "argv", ["x", f.name]), \
                mock.patch("builtins.print"):
            try:
                release_decision.main()
            except SystemExit as e:
                code = e.code
        os.unlink(f.name)
        return code or 0, posted, slept

    PR_EVENT = {"pull_request": {"number": 9}}

    def test_a_pr_event_judges_its_pr_against_the_recorded_push(self):
        for name in ("pull_request_target", "pull_request"):
            self.assertEqual(self.run_main(name, self.PR_EVENT, pr(comments=[comment()]), [push()])[0], 0)
            self.assertEqual(self.run_main(name, self.PR_EVENT, pr(), [push()])[0], 1)
            # pushed after the comment
            self.assertEqual(self.run_main(name, self.PR_EVENT, pr(comments=[comment()]),
                                           [push(at="2026-11-02T11:30:00Z")])[0], 1)
            # no recorded push, and a fork's branch isn't read at all
            self.assertEqual(self.run_main(name, self.PR_EVENT, pr(comments=[comment()]), [])[0], 1)
            self.assertEqual(self.run_main(name, self.PR_EVENT, pr(fork=True, comments=[comment()]), [push()])[0], 1)
        self.assertEqual(self.run_main("pull_request_target", self.PR_EVENT, ordinary())[0], 0)

    COMMENT = {"issue": {"number": 9, "state": "open", "pull_request": {}}}

    def run_of(self, id, name="release-decision-caller", event="pull_request_target", status="completed"):
        return {"id": id, "name": name, "event": event, "status": status,
                "html_url": f"https://github.com/akenon-studio/peras/actions/runs/{id}"}

    def test_a_comment_on_an_open_release_pr_reruns_the_latest_pr_run_on_its_head(self):
        runs = [self.run_of(1), self.run_of(3), self.run_of(4, event="issue_comment"),
                self.run_of(5, name="pr-title-caller"), self.run_of(2, name="release-decision")]
        code, posted, _ = self.run_main("issue_comment", self.COMMENT, pr(), runs=runs)
        self.assertEqual(code, 0)
        self.assertEqual(posted, ["repos/akenon-studio/peras/actions/runs/3/rerun"])
        # a run from an org ruleset's required workflow is found the same way
        _, posted, _ = self.run_main("issue_comment", self.COMMENT, pr(), runs=[self.run_of(7, name="release-decision")])
        self.assertEqual(posted, ["repos/akenon-studio/peras/actions/runs/7/rerun"])

    def test_a_run_still_going_is_rerun_once_it_finishes(self):
        polls = [self.run_of(3, status="in_progress"), self.run_of(3)]
        code, posted, slept = self.run_main("issue_comment", self.COMMENT, pr(),
                                            runs=[self.run_of(3, status="queued")], polls=polls)
        self.assertEqual((code, len(slept)), (0, 2))
        self.assertEqual(posted, ["repos/akenon-studio/peras/actions/runs/3/rerun"])

    def test_a_run_that_never_finishes_fails_the_comments_run(self):
        polls = [self.run_of(3, status="in_progress")] * release_decision.POLLS
        code, posted, _ = self.run_main("issue_comment", self.COMMENT, pr(),
                                        runs=[self.run_of(3, status="in_progress")], polls=polls)
        self.assertNotEqual(code, 0)
        self.assertEqual(posted, [])

    def test_any_other_comment_reruns_nothing(self):
        for event, the_pr, runs in (({"issue": {"number": 9, "state": "open"}}, pr(), [self.run_of(3)]),  # an issue
                                    ({"issue": {"number": 9, "state": "closed", "pull_request": {}}}, pr(), [self.run_of(3)]),
                                    (self.COMMENT, ordinary(), [self.run_of(3)]),
                                    (self.COMMENT, pr(), [])):  # no run on the head yet
            code, posted, _ = self.run_main("issue_comment", event, the_pr, runs=runs)
            self.assertEqual(code, 0)
            self.assertEqual(posted, [], event)

    def test_another_event_fails(self):
        self.assertNotEqual(self.run_main("workflow_dispatch", {}, pr())[0], 0)
        self.assertNotEqual(self.run_main("push", {}, pr())[0], 0)


if __name__ == "__main__":
    unittest.main()

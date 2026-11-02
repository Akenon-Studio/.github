"""Tests for scripts/release_decision.py: a Release PR merges only on a partner's own comment newer
than its head commit (design 6.3). Run: python3 -m unittest discover tests"""

import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

import release_decision  # noqa: E402
from release_decision import decision, is_release_pr, problems  # noqa: E402

PEOPLE = {"ruvinduh", "thytus777"}
COMMITTED = "2026-11-02T10:00:00Z"


def comment(login="RuvinduH", body="Cut it.", at="2026-11-02T11:00:00Z", kind="User"):
    return {"author": {"__typename": kind, "login": login}, "body": body, "createdAt": at,
            "url": f"https://github.com/x/y/pull/9#c-{login}-{at}"}


def pr(author="akenon-studio-release", kind="Bot", branch="release-please--branches--main", labels=(),
       ever=(), comments=(), committed=COMMITTED, state="OPEN", fork=False, sha="abc"):
    return {"number": 9, "url": "https://github.com/x/y/pull/9", "state": state, "isCrossRepository": fork,
            "headRefName": branch, "headRefOid": sha,
            "author": {"__typename": kind, "login": author},
            "labels": {"nodes": [{"name": l} for l in labels]},
            "timelineItems": {"nodes": [{"label": {"name": l}} for l in ever]},
            "commits": {"nodes": [{"commit": {"oid": sha, "committedDate": committed}}]},
            "comments": {"nodes": list(comments)}}


def ordinary(**kw):
    return pr(author="RuvinduH", kind="User", branch="feat/1-x", **kw)


class WhatIsAReleasePrTest(unittest.TestCase):
    def test_the_release_apps_pr(self):
        self.assertTrue(is_release_pr(pr(branch="other")))
        self.assertTrue(is_release_pr(pr(author="akenon-studio-release[bot]", branch="other")))

    def test_a_person_named_like_the_app_is_not_it(self):
        self.assertFalse(is_release_pr(pr(kind="User", branch="other")))

    def test_a_release_please_branch(self):
        self.assertTrue(is_release_pr(ordinary() | {"headRefName": "release-please--branches--main"}))

    def test_the_label_now_or_ever(self):
        self.assertTrue(is_release_pr(ordinary(labels=["autorelease: pending"])))
        # taking the label off doesn't make it an ordinary PR
        self.assertTrue(is_release_pr(ordinary(ever=["autorelease: pending"])))
        self.assertFalse(is_release_pr(ordinary(labels=["autorelease: tagged"], ever=["release"])))

    def test_any_other_pr(self):
        self.assertFalse(is_release_pr(ordinary()))
        self.assertFalse(is_release_pr(pr(author="renovate", branch="renovate/x")))


class DecisionTest(unittest.TestCase):
    def test_a_partners_own_comment_after_the_head_commit_passes(self):
        self.assertEqual(problems(pr(comments=[comment()]), PEOPLE), [])
        self.assertEqual(problems(pr(comments=[comment(login="thytus777")]), PEOPLE), [])
        self.assertEqual(problems(pr(comments=[comment(at=COMMITTED)]), PEOPLE), [])  # the same second

    def test_no_comment_fails(self):
        found = problems(pr(), PEOPLE)
        self.assertEqual(len(found), 1)
        self.assertIn("#9", found[0])
        self.assertIn("design 6.3", found[0])

    def test_a_comment_older_than_the_head_commit_fails(self):
        # every push changes what would ship, so it needs a new comment
        self.assertEqual(len(problems(pr(comments=[comment(at="2026-11-02T09:59:59Z")]), PEOPLE)), 1)

    def test_an_agents_comment_never_counts(self):
        for body in ("<!-- agent -->\nCut it.", "Cut it. <!--agent-->", "<!-- AGENT -->"):
            self.assertEqual(len(problems(pr(comments=[comment(body=body)]), PEOPLE)), 1, body)

    def test_only_a_person_in_the_teams_counts(self):
        self.assertEqual(len(problems(pr(comments=[comment(login="outsider")]), PEOPLE)), 1)
        # bots never count, even one with a partner's name
        self.assertEqual(len(problems(pr(comments=[comment(login="ruvinduh", kind="Bot")]), PEOPLE)), 1)
        self.assertEqual(len(problems(pr(comments=[{"author": None, "body": "x", "createdAt": "2026-11-02T12:00:00Z"}]),
                                      PEOPLE)), 1)  # a deleted account

    def test_the_newest_counting_comment_is_the_decision(self):
        older, newer = comment(at="2026-11-02T11:00:00Z"), comment(login="Thytus777", at="2026-11-02T12:00:00Z")
        self.assertIs(decision(pr(comments=[older, newer, comment(body="<!-- agent -->")]), PEOPLE), newer)

    def test_every_other_pr_passes(self):
        self.assertEqual(problems(ordinary(), PEOPLE), [])
        self.assertEqual(problems(pr(author="renovate", branch="renovate/x"), PEOPLE), [])

    def test_the_partners_come_from_the_teams_file(self):
        self.assertTrue(release_decision.org_people())  # rules.org_people(), as issue_fields.py reads them


class EventsTest(unittest.TestCase):
    """main(): which PR each event judges, and what a comment does."""

    def run_main(self, event_name, event, prs=(), env=None):
        calls, ran = [], []

        def graphql(query, **variables):
            calls.append(variables)
            if "pullRequests(" in query:
                return {"repository": {"pullRequests": {"nodes": list(prs)}}}
            return {"repository": {"pullRequest": next(p for p in prs if p["number"] == variables["i"])}}

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(event, f)
        environ = {"GITHUB_EVENT_NAME": event_name, "GITHUB_REPOSITORY": "akenon-studio/peras",
                   "GITHUB_REF_NAME": "release-please--branches--main", "GITHUB_SHA": "abc", **(env or {})}
        code = 0
        with mock.patch.object(release_decision, "graphql", graphql), \
                mock.patch.object(release_decision, "org_people", lambda: PEOPLE), \
                mock.patch.object(release_decision.subprocess, "run", lambda cmd, **kw: ran.append(cmd)), \
                mock.patch.dict(os.environ, environ), mock.patch.object(sys, "argv", ["x", f.name]), \
                mock.patch("builtins.print"):
            try:
                release_decision.main()
            except SystemExit as e:
                code = e.code
        os.unlink(f.name)
        return code, calls, ran

    def test_a_pr_event_judges_its_pr(self):
        self.assertEqual(self.run_main("pull_request", {"pull_request": {"number": 9}}, [pr()])[0], 1)
        self.assertIn(self.run_main("pull_request", {"pull_request": {"number": 9}},
                                    [pr(comments=[comment()])])[0], (0, None))

    def test_a_dispatch_finds_its_pr_by_branch_and_commit_never_an_input(self):
        good = pr(comments=[comment()])
        code, calls, _ = self.run_main("workflow_dispatch", {"inputs": {"pr": 1}}, [good])
        self.assertIn(code, (0, None))
        self.assertEqual(calls[0]["b"], "release-please--branches--main")
        # a PR whose head moved on, or a fork's PR of the same branch name, isn't this run's
        for other in (pr(sha="def", comments=[comment()]), pr(fork=True, comments=[comment()])):
            code, _, _ = self.run_main("workflow_dispatch", {}, [other])
            self.assertNotIn(code, (0, None))
        self.assertEqual(self.run_main("workflow_dispatch", {}, [pr()])[0], 1)  # found, no decision

    def test_a_comment_on_an_open_release_pr_starts_the_caller_on_its_branch(self):
        event = {"issue": {"number": 9, "state": "open", "pull_request": {}}}
        code, _, ran = self.run_main("issue_comment", event, [pr()])
        self.assertIn(code, (0, None))
        self.assertEqual(ran, [["gh", "workflow", "run", "release-decision-caller.yml", "-R",
                                "akenon-studio/peras", "--ref", "release-please--branches--main"]])

    def test_any_other_comment_starts_nothing(self):
        for event, prs in (({"issue": {"number": 9, "state": "open"}}, []),  # an issue
                           ({"issue": {"number": 9, "state": "closed", "pull_request": {}}}, [pr()]),
                           ({"issue": {"number": 9, "state": "open", "pull_request": {}}}, [ordinary()]),
                           ({"issue": {"number": 9, "state": "open", "pull_request": {}}}, [pr(fork=True)])):
            code, _, ran = self.run_main("issue_comment", event, prs)
            self.assertIn(code, (0, None))
            self.assertEqual(ran, [], event)

    def test_another_event_fails(self):
        self.assertNotIn(self.run_main("push", {})[0], (0, None))


if __name__ == "__main__":
    unittest.main()

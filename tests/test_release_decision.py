"""Tests for scripts/release_decision.py: a Release PR merges only on a partner's own comment newer
than the push that made its head commit its head (design 6.3). Run: python3 -m unittest discover tests"""

import json
import os
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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

    def test_a_mandated_sessions_comment_naming_the_commenters_own_mandate_passes(self):
        line = "Decision: the agent's call, taken under @RuvinduH's mandate over Akenon-Studio/handbook#142."
        for body in (f"<!-- agent -->\nCutting it.\n\n{line}", f"Cut. <!--agent-->\n{line}",
                     f"<!-- agent -->\n{line.replace(chr(39), chr(0x2019))}"):
            self.assertEqual(problems(pr(comments=[comment(body=body)]), PEOPLE, PUSHED), [], body)
        # the login's case doesn't matter
        lower = f"<!-- agent -->\n{line.replace('@RuvinduH', '@ruvinduh')}"
        self.assertEqual(problems(pr(comments=[comment(body=lower)]), PEOPLE, PUSHED), [])

    def test_a_marked_comment_naming_another_logins_mandate_fails(self):
        body = "<!-- agent -->\nDecision: the agent's call, taken under @Thytus777's mandate over handbook#142."
        self.assertEqual(len(problems(pr(comments=[comment(body=body)]), PEOPLE, PUSHED)), 1)

    def test_a_marked_comment_without_a_mandate_decision_line_fails(self):
        for body in ("<!-- agent -->\nDecision: cut it.",
                     "<!-- agent -->\nAs under @RuvinduH's mandate over handbook#142, cut it.",
                     "<!-- agent -->\nDecision: the agent's call, taken under @RuvinduH's mandate over"):
            self.assertEqual(len(problems(pr(comments=[comment(body=body)]), PEOPLE, PUSHED)), 1, body)

    def test_a_mandated_comment_older_than_the_push_fails(self):
        body = "<!-- agent -->\nDecision: the agent's call, taken under @RuvinduH's mandate over handbook#142."
        old = comment(body=body, at="2026-11-02T09:59:59Z")
        self.assertEqual(len(problems(pr(comments=[old]), PEOPLE, PUSHED)), 1)

    def test_a_mandated_comment_from_outside_the_teams_fails(self):
        body = "<!-- agent -->\nDecision: the agent's call, taken under @outsider's mandate over handbook#142."
        self.assertEqual(len(problems(pr(comments=[comment(login="outsider", body=body)]), PEOPLE, PUSHED)), 1)

    def test_the_failure_names_both_ways_to_record_the_decision(self):
        found = problems(pr(), PEOPLE, PUSHED)[0]
        self.assertIn("without the agent marker", found)
        self.assertIn("mandate over", found)
        self.assertIn("7.12", found)

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
    """main(): what each mode does with its event."""

    def run_main(self, mode, event, the_pr, activity=(), runs=(), polls=None, refused=0):
        posted, slept = [], []
        polls = iter(polls or [])
        refusals = iter(range(refused))

        def gh(path, *args, **kw):
            if args == ("-X", "POST"):
                if next(refusals, None) is not None:
                    sys.exit(f"gh api {path} failed:\nThis workflow is already running (HTTP 403)")
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
        code = 0
        with mock.patch.object(release_decision, "graphql",
                               lambda q, **v: {"repository": {"pullRequest": the_pr}}), \
                mock.patch.object(release_decision, "gh", gh), \
                mock.patch.object(release_decision, "org_people", lambda: PEOPLE), \
                mock.patch.object(release_decision.time, "sleep", slept.append), \
                mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "akenon-studio/peras"}), \
                mock.patch.object(sys, "argv", ["x", mode, f.name]), mock.patch("builtins.print"):
            try:
                release_decision.main()
            except SystemExit as e:
                code = e.code
        os.unlink(f.name)
        return code or 0, posted, slept

    PR_EVENT = {"pull_request": {"number": 9}}

    def test_judge_judges_the_events_pr_against_the_recorded_push(self):
        self.assertEqual(self.run_main("judge", self.PR_EVENT, pr(comments=[comment()]), [push()])[0], 0)
        self.assertEqual(self.run_main("judge", self.PR_EVENT, pr(), [push()])[0], 1)
        # pushed after the comment
        self.assertEqual(self.run_main("judge", self.PR_EVENT, pr(comments=[comment()]),
                                       [push(at="2026-11-02T11:30:00Z")])[0], 1)
        # no recorded push, and a fork's branch isn't read at all
        self.assertEqual(self.run_main("judge", self.PR_EVENT, pr(comments=[comment()]), [])[0], 1)
        self.assertEqual(self.run_main("judge", self.PR_EVENT, pr(fork=True, comments=[comment()]), [push()])[0], 1)
        self.assertEqual(self.run_main("judge", self.PR_EVENT, ordinary())[0], 0)

    COMMENT = {"issue": {"number": 9, "state": "open", "pull_request": {}}}
    LABEL = {"pull_request": {"number": 9}, "label": {"name": "autorelease: pending"}}

    def run_of(self, id, name="release-decision", event="pull_request", status="completed",
               path=".github/workflows/release-decision.yml", workflow=100, days_old=1):
        created = (datetime.now(timezone.utc) - timedelta(days=days_old)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"id": id, "name": name, "event": event, "status": status, "path": path,
                "workflow_id": workflow, "created_at": created,
                "html_url": f"https://github.com/akenon-studio/peras/actions/runs/{id}"}

    def test_a_ruleset_run_in_another_repo_is_found_by_the_source_workflows_name_and_path(self):
        # The shape GitHub gave "Action pins"' ruleset run 41441981880 in peras (2026-11-02), a repo
        # with no action-pins.yml of its own: the source workflow's name and path, its own
        # workflow_id, on the pull_request event.
        run = {"id": 41441981880, "name": "release-decision", "path": ".github/workflows/release-decision.yml",
               "workflow_id": 383735657, "event": "pull_request", "status": "completed",
               "created_at": self.run_of(1)["created_at"],
               "html_url": "https://github.com/Akenon-Studio/peras/actions/runs/41441981880"}
        _, posted, _ = self.run_main("rerun", self.COMMENT, pr(), runs=[run])
        self.assertEqual(posted, ["repos/akenon-studio/peras/actions/runs/41441981880/rerun"])

    def test_a_comment_or_label_on_an_open_release_pr_reruns_the_latest_ruleset_run_on_its_head(self):
        runs = [self.run_of(1), self.run_of(3), self.run_of(4, event="issue_comment"),
                self.run_of(5, name="pr-title-caller"), self.run_of(6, path=".github/workflows/other.yml"),
                self.run_of(2)]
        for event in (self.COMMENT, self.LABEL):
            code, posted, _ = self.run_main("rerun", event, pr(), runs=runs)
            self.assertEqual(code, 0)
            self.assertEqual(posted, ["repos/akenon-studio/peras/actions/runs/3/rerun"])

    def test_a_repos_own_workflow_of_the_same_name_cant_hide_the_rulesets_run(self):
        runs = [self.run_of(3), self.run_of(8, workflow=200)]
        _, posted, _ = self.run_main("rerun", self.COMMENT, pr(), runs=runs)
        self.assertEqual(posted, ["repos/akenon-studio/peras/actions/runs/3/rerun",
                                  "repos/akenon-studio/peras/actions/runs/8/rerun"])

    def test_a_run_still_going_is_rerun_once_it_finishes(self):
        polls = [self.run_of(3, status="in_progress"), self.run_of(3)]
        code, posted, slept = self.run_main("rerun", self.COMMENT, pr(),
                                            runs=[self.run_of(3, status="queued")], polls=polls)
        self.assertEqual((code, len(slept)), (0, 2))
        self.assertEqual(posted, ["repos/akenon-studio/peras/actions/runs/3/rerun"])

    def test_a_run_that_finishes_at_the_last_poll_is_rerun(self):
        polls = [self.run_of(3, status="in_progress")] * (release_decision.POLLS - 1) + [self.run_of(3)]
        code, posted, _ = self.run_main("rerun", self.COMMENT, pr(),
                                        runs=[self.run_of(3, status="in_progress")], polls=polls)
        self.assertEqual(code, 0)
        self.assertEqual(posted, ["repos/akenon-studio/peras/actions/runs/3/rerun"])

    def test_a_rerun_another_comment_started_first_is_waited_for_and_rerun_again(self):
        polls = [self.run_of(3, status="in_progress"), self.run_of(3)]
        code, posted, slept = self.run_main("rerun", self.COMMENT, pr(), runs=[self.run_of(3)],
                                            polls=polls, refused=1)
        self.assertEqual((code, len(slept)), (0, 2))
        self.assertEqual(posted, ["repos/akenon-studio/peras/actions/runs/3/rerun"])

    def test_a_run_too_old_to_rerun_says_to_close_and_reopen(self):
        with mock.patch("builtins.print"):
            code, posted, _ = self.run_main("rerun", self.COMMENT, pr(), runs=[self.run_of(3, days_old=31)])
        self.assertIn("close and reopen #9", str(code))
        self.assertEqual(posted, [])

    def test_a_run_that_never_finishes_fails_the_reruns_run(self):
        polls = [self.run_of(3, status="in_progress")] * release_decision.POLLS
        code, posted, _ = self.run_main("rerun", self.COMMENT, pr(),
                                        runs=[self.run_of(3, status="in_progress")], polls=polls)
        self.assertNotEqual(code, 0)
        self.assertEqual(posted, [])

    def test_any_other_comment_or_label_reruns_nothing(self):
        for event, the_pr, runs in (({"issue": {"number": 9, "state": "open"}}, pr(), [self.run_of(3)]),  # an issue
                                    ({"issue": {"number": 9, "state": "closed", "pull_request": {}}}, pr(), [self.run_of(3)]),
                                    (self.COMMENT, ordinary(), [self.run_of(3)]),
                                    (self.LABEL, ordinary(), [self.run_of(3)]),
                                    (self.LABEL, pr(state="CLOSED"), [self.run_of(3)]),
                                    (self.COMMENT, pr(), [])):  # no run on the head yet
            code, posted, _ = self.run_main("rerun", event, the_pr, runs=runs)
            self.assertEqual(code, 0)
            self.assertEqual(posted, [], event)

    def test_another_mode_fails(self):
        self.assertNotEqual(self.run_main("dispatch", {}, pr())[0], 0)


if __name__ == "__main__":
    unittest.main()

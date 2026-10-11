"""Tests for scripts/automation_failures.py (design 6.8). Run: python3 -m unittest discover tests"""

import datetime
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
from automation_failures import (REPO, body, failed_schedule, lane_idle, renovate_pages,  # noqa: E402
                                 renovate_problems, repository_problems, run_again, sections)
from issue_fields import check, load_forms  # noqa: E402
from rules import status_page  # noqa: E402

NOW = datetime.datetime(2026, 10, 20, tzinfo=datetime.timezone.utc)
PAGES = renovate_pages()

DASHBOARD = """This issue lists Renovate updates and detected dependencies.

## Repository problems

These problems occurred while renovating this repository. [View logs](https://developer.mend.io/x).

 - WARN: Error checking last author for isBranchModified

## Open

- [ ] <!-- rebase-branch=renovate/x -->chore(deps): x

---

- [ ] <!-- manual job -->Check this box to trigger a request for Renovate to run again on this repository
"""


def run(conclusion):
    return {"name": "issue-sweep", "conclusion": conclusion, "html_url": "https://x",
            "created_at": "2026-10-20T00:00:00Z"}


def issue(title, body="", login="renovate[bot]"):
    return {"title": title, "body": body, "number": 9, "html_url": "https://x",
            "user": {"login": login, "type": "Bot"}}


class ScheduledRunsTest(unittest.TestCase):
    def test_a_failed_latest_finished_run(self):
        for bad in ("failure", "timed_out", "startup_failure"):
            self.assertEqual(failed_schedule(run(bad))["conclusion"], bad)
        for fine in ("success", "cancelled", "skipped"):
            self.assertIsNone(failed_schedule(run(fine)))
        self.assertIsNone(failed_schedule(None))  # never ran

    def test_the_report_is_private(self):
        self.assertEqual(REPO, "akenon-studio/.github-private")


class RenovateTest(unittest.TestCase):
    def test_the_problems_section(self):
        self.assertEqual(repository_problems(DASHBOARD),
                         ["WARN: Error checking last author for isBranchModified"])
        self.assertEqual(repository_problems("## Open\n\n- [ ] x"), [])

    def test_the_run_again_box(self):
        ticked, text = run_again(DASHBOARD)
        self.assertFalse(ticked)
        self.assertIn("- [x] <!-- manual job -->", text)
        self.assertIn("- [ ] <!-- rebase-branch", text)  # only that box
        self.assertTrue(run_again(text)[0])
        self.assertIsNone(run_again("no box"))

    def test_a_healthy_dashboard_is_ticked_and_its_problems_listed(self):
        problems, tick = renovate_problems("platform", [issue(PAGES[0], DASHBOARD)], PAGES)
        self.assertEqual(len(problems), 1)
        self.assertIn("isBranchModified", problems[0])
        self.assertEqual(tick, 9)

    def test_a_box_still_ticked_means_renovate_isnt_running(self):
        problems, tick = renovate_problems("platform", [issue(PAGES[0], run_again(DASHBOARD)[1])], PAGES)
        self.assertIsNone(tick)
        self.assertIn("hasn't run", problems[-1])

    def test_no_dashboard_and_a_config_error(self):
        problems, tick = renovate_problems("animations", [issue(PAGES[1])], PAGES)
        self.assertIsNone(tick)
        self.assertEqual(len(problems), 2)
        self.assertIn("config is broken", problems[0])
        self.assertIn("no Renovate dashboard", problems[1])

    def test_someone_elses_issue_with_the_title_doesnt_count(self):
        problems, _ = renovate_problems("x", [issue(PAGES[0], DASHBOARD, login="someone")], PAGES)
        self.assertIn("no Renovate dashboard", problems[0])

    def test_the_issue_checks_skip_only_renovates_status_pages(self):
        bot = {"__typename": "Bot", "login": "renovate"}
        self.assertTrue(status_page(bot, PAGES[0]))
        self.assertTrue(status_page({"type": "Bot", "login": "renovate[bot]"}, PAGES[1]))
        self.assertFalse(status_page(bot, "chore(deps): x"))
        self.assertFalse(status_page({"__typename": "User", "login": "renovate"}, PAGES[0]))


def pr(comment, said="2026-10-15T00:00:00Z", pushed="2026-10-14T00:00:00Z", labels=(),
       later=(), reviews=(), checks="2026-10-14T00:00:00Z"):
    """`later`: (author type, body, time) comments after the lane's."""
    comments = [{"body": comment, "createdAt": said, "author": {"__typename": "Bot"}}]
    comments += [{"body": b, "createdAt": at, "author": {"__typename": who}} for who, b, at in later]
    return {"comments": {"nodes": comments},
            "reviews": {"nodes": [{"createdAt": at, "author": {"__typename": "User"}} for at in reviews]},
            "commits": {"nodes": [{"commit": {"committedDate": pushed,
                                              "checkSuites": {"nodes": [{"createdAt": checks}]}}}]},
            "labels": {"nodes": [{"name": l} for l in labels]}}


DROPPED = "<!-- merge-lane -->\n@x Taken out of the merge lane: a required check failed."


class LaneTest(unittest.TestCase):
    def test_taken_out_and_left(self):
        self.assertTrue(lane_idle(pr(DROPPED), NOW))

    def test_recent_pushed_since_relabelled_or_talked_about(self):
        self.assertFalse(lane_idle(pr(DROPPED, said="2026-10-19T00:00:00Z"), NOW))
        self.assertFalse(lane_idle(pr(DROPPED, pushed="2026-10-16T00:00:00Z"), NOW))
        self.assertFalse(lane_idle(pr(DROPPED, labels=["ready-to-merge"]), NOW))
        self.assertFalse(lane_idle(pr("Looking at it."), NOW))
        self.assertFalse(lane_idle(pr(DROPPED, later=[("User", "On it", "2026-10-16T00:00:00Z")]), NOW))
        self.assertFalse(lane_idle(pr(DROPPED, reviews=["2026-10-16T00:00:00Z"]), NOW))
        # an old commit pushed after the comment: its checks started then
        self.assertFalse(lane_idle(pr(DROPPED, checks="2026-10-16T00:00:00Z"), NOW))

    def test_a_bots_comment_after_it_isnt_activity(self):
        notice = ("Bot", "<!-- pr-conflicts -->\n@x This PR conflicts", "2026-10-15T00:10:00Z")
        self.assertTrue(lane_idle(pr(DROPPED, later=[notice]), NOW))


class BodyTest(unittest.TestCase):
    STATE = {"runs": [{"repo": ".github", "workflow": "issue-sweep", "conclusion": "failure",
                       "url": "https://x", "at": "2026-10-19T00:00:00Z"}],
             "secrets": [], "unread": ["secret-scanning alerts (the settings app needs it)"]}

    def test_nothing_failed_closes_it(self):
        self.assertIsNone(body(sections({"runs": [], "secrets": [], "unread": []}, [], [])))

    def test_body_is_a_valid_task_form_listing_only_whats_there(self):
        text = body(sections(self.STATE, ["platform: WARN: x"], []))
        self.assertEqual(check("Task", text, load_forms()), [])
        self.assertIn("**Scheduled runs that failed**", text)
        self.assertIn("**Couldn't check**", text)
        self.assertNotIn("merge lane", text.split("### Why")[0])


if __name__ == "__main__":
    unittest.main()

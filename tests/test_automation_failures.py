"""Tests for scripts/automation_failures.py (design 6.8). Run: python3 -m unittest discover tests"""

import datetime
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import automation_failures  # noqa: E402
from automation_failures import (body, failed_run, lane_idle, renovate_pages,  # noqa: E402
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
            self.assertEqual(failed_run(run(bad), NOW)["conclusion"], bad)
        for fine in ("success", "cancelled", "skipped"):
            self.assertIsNone(failed_run(run(fine), NOW))
        self.assertIsNone(failed_run(None, NOW))  # never ran
        old = {**run("failure"), "created_at": "2026-08-01T00:00:00Z"}  # its cron is long gone
        self.assertIsNone(failed_run(old, NOW))


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

    def test_our_own_renovates_dashboard_counts_too(self):
        ours = issue(PAGES[0], DASHBOARD, login="akenon-studio-renovate[bot]")
        problems, tick = renovate_problems("platform", [ours], PAGES)
        self.assertEqual(len(problems), 1)
        self.assertIn("isBranchModified", problems[0])
        self.assertIsNone(tick)  # its box, if it had one, isn't Mend's: never ticked, never "not running"
        problems, _ = renovate_problems("platform", [issue(PAGES[0], run_again(DASHBOARD)[1],
                                                           login="akenon-studio-renovate[bot]")], PAGES)
        self.assertFalse([p for p in problems if "hasn't run" in p])

    def test_two_renovates_on_one_repo(self):
        both = [issue(PAGES[0], DASHBOARD), issue(PAGES[0], "## Open\n", login="akenon-studio-renovate[bot]")]
        problems, tick = renovate_problems("platform", both, PAGES)
        self.assertIn("two Renovates", problems[0])
        self.assertIsNone(tick)  # ours is read, so Mend's stale page isn't ticked

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
       later=(), reviews=(), forced=None):
    """`later`: (author type, body, time) comments after the lane's."""
    comments = [{"body": comment, "createdAt": said, "author": {"__typename": "Bot"}}]
    comments += [{"body": b, "createdAt": at, "author": {"__typename": who}} for who, b, at in later]
    return {"comments": {"nodes": comments},
            "reviews": {"nodes": [{"createdAt": at, "author": {"__typename": "User"}} for at in reviews]},
            "commits": {"nodes": [{"commit": {"committedDate": pushed}}]},
            "forcePushes": {"nodes": [{"createdAt": forced}] if forced else []},
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
        self.assertFalse(lane_idle(pr(DROPPED, forced="2026-10-16T00:00:00Z"), NOW))

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
        self.assertIn("**Runs that failed**", text)
        self.assertIn("**Couldn't check**", text)
        self.assertNotIn("merge lane", text.split("### Why")[0])
        # each section starts its own paragraph, so its heading doesn't join the list above (#145)
        self.assertIn("\n\n**Couldn't check**", text)


class CollectTest(unittest.TestCase):
    def test_renovate_repos_unreadable_repos_and_disabled_schedules(self):
        def try_gh(*args):
            path = args[-1]
            if path.endswith("/actions/workflows?per_page=100"):
                return {"workflows": [
                    {"id": 1, "name": "sweep", "state": "active", "html_url": "u", "updated_at": "t"},
                    {"id": 2, "name": "scorecard", "state": "disabled_inactivity", "html_url": "u",
                     "updated_at": "2026-10-01T00:00:00Z"}]}
            if "/runs?" in path:
                return {"workflow_runs": [run("failure")]}
            self.assertIn("hide_secret=true", path)  # never fetch the secret itself
            return [[{"repository": {"name": "a"}, "secret_type": "k", "html_url": "u",
                      "created_at": "t"},
                     {"repository": {"name": "old-repo"}, "secret_type": "k", "html_url": "u",
                      "created_at": "t"}]]
        set_up = {"a": True, "b": False, "c": None}
        with mock.patch.multiple(automation_failures, managed_repos=lambda: ["a", "b", "c"],
                                 renovate_set_up=lambda r: set_up[r], try_gh=try_gh), \
                tempfile.NamedTemporaryFile(suffix=".json") as f, \
                mock.patch("sys.stdout") as out:
            automation_failures.collect(f.name, NOW)
            state = json.load(open(f.name))
        printed = "".join(c.args[0] for c in out.write.call_args_list)
        self.assertNotIn("secret", printed)  # this log is public
        self.assertEqual([a["repo"] for a in state["secrets"]], ["a"])  # managed repos only
        self.assertEqual(state["no_config"], ["b"])
        self.assertEqual(state["renovate"], ["a"])
        self.assertEqual(len(state["unread"]), 1)
        self.assertIn("whether Renovate is set up in c", state["unread"][0])
        self.assertEqual(sorted(r["conclusion"] for r in state["runs"] if r["repo"] == "a"),
                         ["disabled by GitHub for inactivity", "failure"])

    def test_report_jobs_after_main_are_read_too(self):
        """A failed peras-tests run on main is listed: it runs on a push, not a schedule (#235)."""
        asked = []

        def try_gh(*args):
            path = args[-1]
            if path.endswith("/actions/workflows?per_page=100"):
                return {"workflows": [
                    {"id": 1, "name": "peras-tests", "path": ".github/workflows/peras-tests.yml",
                     "state": "active", "html_url": "u", "updated_at": "t"},
                    {"id": 2, "name": "tests", "path": ".github/workflows/tests.yml",
                     "state": "active", "html_url": "u", "updated_at": "t"}]}
            if "/runs?" in path:
                asked.append(path)
                failed = "/workflows/1/" in path and "event=push" in path
                return {"workflow_runs": [{**run("failure" if failed else "success"),
                                           "name": "peras-tests"}]}
            return []
        with mock.patch.multiple(automation_failures, managed_repos=lambda: [".github", "peras"],
                                 renovate_set_up=lambda r: True, try_gh=try_gh), \
                tempfile.NamedTemporaryFile(suffix=".json") as f, mock.patch("sys.stdout"):
            automation_failures.collect(f.name, NOW)
            state = json.load(open(f.name))
        self.assertEqual([(r["repo"], r["workflow"]) for r in state["runs"]],
                         [(".github", "peras-tests")])
        push = [p for p in asked if "event=push" in p]
        self.assertEqual(len(push), 1)  # peras-tests in .github only: not CI, not other repos
        self.assertIn("/workflows/1/runs?event=push&branch=main&status=completed&per_page=1",
                      push[0])

    def test_after_main_jobs_exist_and_run_on_a_push_to_main(self):
        import yaml
        root = pathlib.Path(__file__).resolve().parent.parent
        for path in automation_failures.AFTER_MAIN[".github"]:
            on = yaml.safe_load((root / path).read_text())[True]  # YAML reads `on:` as True
            self.assertIn("main", on["push"]["branches"], path)
            self.assertNotIn("schedule", on, path)

    def test_where_renovate_config_can_be(self):
        from automation_failures import renovate_set_up
        self.assertTrue(renovate_set_up("x", lambda r, p: p == ".github/renovate.json"))
        self.assertFalse(renovate_set_up("x", lambda r, p: False))
        self.assertIsNone(renovate_set_up("x", lambda r, p: None if p == "renovate.json" else False))

    def test_has_file_tells_missing_from_unreadable(self):
        from automation_failures import has_file
        done = lambda code, err="": mock.Mock(returncode=code, stderr=err)
        with mock.patch("subprocess.run", return_value=done(0)):
            self.assertTrue(has_file("x", "renovate.json"))
        with mock.patch("subprocess.run", return_value=done(1, "gh: Not Found (HTTP 404)")):
            self.assertFalse(has_file("x", "renovate.json"))
        with mock.patch("subprocess.run", return_value=done(1, "gh: Resource not accessible (HTTP 403)")):
            self.assertIsNone(has_file("x", "renovate.json"))


class ReportTest(unittest.TestCase):
    def run_report_unpatched_lane(self, issues):
        calls = []
        state = {"runs": [], "secrets": [], "unread": [], "renovate": ["platform", "peras"]}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(state, f)
        with mock.patch.multiple(automation_failures, managed_repos=lambda: ["platform", "peras"],
                                 try_gh=lambda p: issues.get(p.split("/")[2]) if "creator=renovate[bot]" in p else ([] if issues.get(p.split("/")[2]) is not None else None),
                                 keep=lambda repo, label, title, text, passed=None: calls.append(
                                     ("keep", repo, text)),
                                 gh=lambda *a, **k: self.fail("nothing to tick")):
            automation_failures.report(f.name, NOW)
        return calls

    def run_report(self, issues, fresh_body):
        calls = []
        state = {"runs": [], "secrets": [], "unread": [], "renovate": ["platform", "peras"]}

        def keep(repo, label, title, text, passed=None):
            calls.append(("keep", repo, text))
            return "kept"

        def gh(path, *args, body=None):
            calls.append(("gh", path, args, body))
            return {"body": fresh_body}

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(state, f)
        with mock.patch.multiple(automation_failures, managed_repos=lambda: ["platform", "peras"],
                                 try_gh=lambda p: issues.get(p.split("/")[2]) if "creator=renovate[bot]" in p else ([] if issues.get(p.split("/")[2]) is not None else None), keep=keep, gh=gh,
                                 lane_prs=lambda now, managed: []):
            automation_failures.report(f.name, NOW)
        return calls

    def test_reports_then_ticks_a_fresh_copy(self):
        calls = self.run_report({"platform": [issue(PAGES[0], DASHBOARD)], "peras": None},
                                DASHBOARD + "\nnewer")
        self.assertEqual(calls[0][0], "keep")  # the report comes first
        self.assertEqual(calls[0][1], "akenon-studio/.github-private")
        self.assertIn("Renovate's issues in peras", calls[0][2])  # couldn't check
        self.assertIn("isBranchModified", calls[0][2])
        patch = [c for c in calls if c[0] == "gh" and "PATCH" in c[2]]
        self.assertEqual(len(patch), 1)
        self.assertIn("newer", patch[0][3]["body"])  # ticked in the copy read just now
        self.assertIn("- [x] <!-- manual job -->", patch[0][3]["body"])

    def test_a_failed_lane_search_is_listed_and_the_rest_still_filed(self):
        def boom(now, managed):
            raise SystemExit("GraphQL error")
        with mock.patch.object(automation_failures, "lane_prs", boom):
            calls = self.run_report_unpatched_lane({"platform": [], "peras": []})
        self.assertIn("PRs the merge lane took out (the search failed)", calls[0][2])

    def test_our_own_renovates_issues_are_read_too(self):
        calls = []
        state = {"runs": [], "secrets": [], "unread": [], "renovate": ["platform"]}
        ours = issue(PAGES[0], DASHBOARD, login="akenon-studio-renovate[bot]")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(state, f)
        with mock.patch.multiple(automation_failures, managed_repos=lambda: ["platform"],
                                 try_gh=lambda p: [ours] if "creator=akenon-studio-renovate[bot]" in p else [],
                                 keep=lambda repo, label, title, text, passed=None: calls.append(text),
                                 gh=lambda *a, **k: self.fail("nothing to tick"),
                                 lane_prs=lambda now, managed: []):
            automation_failures.report(f.name, NOW)
        self.assertIn("isBranchModified", calls[0])
        self.assertNotIn("no Renovate dashboard", calls[0])

    def test_no_tick_if_renovate_ticked_meanwhile(self):
        calls = self.run_report({"platform": [issue(PAGES[0], DASHBOARD)], "peras": []},
                                run_again(DASHBOARD)[1])
        self.assertFalse([c for c in calls if c[0] == "gh" and "PATCH" in c[2]])


class WorkflowTest(unittest.TestCase):
    def test_the_report_step_uses_an_every_repo_token(self):
        import yaml
        wf = yaml.safe_load((pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows"
                             / "settings-check.yml").read_text())
        steps = {s.get("id") or s.get("name"): s for s in wf["jobs"]["check"]["steps"]}
        step = steps["Open, update or close the automation failures issue"]
        self.assertEqual(step["env"]["GH_TOKEN"], "${{ steps.failures-app.outputs.token }}")
        self.assertNotIn("repositories", steps["failures-app"]["with"])


if __name__ == "__main__":
    unittest.main()

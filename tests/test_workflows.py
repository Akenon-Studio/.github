"""Tests for the reusable workflows every repo calls (design 4.2): their jobs always run, and this
repo calls each one itself. Run: python3 -m unittest discover tests"""

import pathlib
import unittest

import yaml

WORKFLOWS = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows"


def load(name):
    workflow = yaml.safe_load((WORKFLOWS / name).read_text())
    workflow["on"] = workflow.pop(True, workflow.get("on"))  # YAML reads a bare `on:` as True
    return workflow


REUSABLE = {p.name: load(p.name) for p in sorted(WORKFLOWS.glob("*.yml"))
            if "workflow_call" in (load(p.name)["on"] or {})}


class ReusableWorkflowsTest(unittest.TestCase):
    def test_the_shared_checks_are_reusable(self):
        self.assertLessEqual({"pr-title.yml", "issue-fields.yml", "security-scan.yml"}, set(REUSABLE))

    # issue-fields isn't a required check, and its condition reads only the issue, which a PR
    # event doesn't have: it skips Renovate's status pages (design 6.8), never a PR.
    MAY_SKIP = {("issue-fields.yml", "sync")}

    def test_no_job_is_skipped(self):
        # a skipped job reports "skipped", which passes a required check
        for name, workflow in REUSABLE.items():
            for job_id, job in workflow["jobs"].items():
                if (name, job_id) not in self.MAY_SKIP:
                    self.assertNotIn("if", job, f"{name}: job {job_id}")

    def test_issue_fields_skips_exactly_renovates_status_pages(self):
        import json
        bots = json.loads((WORKFLOWS.parent.parent / "rulesets" / "bots.json").read_text())
        condition = REUSABLE["issue-fields.yml"]["jobs"]["sync"]["if"]
        self.assertIn("github.event.issue.user.login == 'renovate[bot]'", condition)
        for title in bots["sources"]["renovate"]["status_pages"]:
            self.assertIn(f"github.event.issue.title == '{title}'", condition)
        self.assertEqual(condition.count("github.event.issue.title =="),
                         len(bots["sources"]["renovate"]["status_pages"]))
        self.assertNotIn("pull_request", condition)

    # ai-review is called from main even here: its sign-in trusts only the main copy (ai-review.yml).
    # merge-lane too: it holds the Merge Lane app's key, so only main's copy runs (merge-lane.yml).
    FROM_MAIN = {"ai-review.yml", "merge-lane.yml"}

    def test_this_repo_calls_each_one_from_its_own_copy(self):
        for name in REUSABLE:
            caller = load(name.replace(".yml", "-caller.yml"))
            uses = [j.get("uses") for j in caller["jobs"].values()]
            want = (f"akenon-studio/.github/.github/workflows/{name}@main" if name in self.FROM_MAIN
                    else f"./.github/workflows/{name}")
            self.assertEqual(uses, [want], name)

    def test_security_scan_checks(self):
        caller = load("security-scan-caller.yml")
        self.assertEqual(list(caller["jobs"]), ["security-scan"])
        self.assertEqual(sorted(REUSABLE["security-scan.yml"]["jobs"]), ["code", "dependencies"])
        self.assertEqual(set(caller["on"]), {"pull_request", "push"})


    def test_one_job_per_check_workflow_and_every_check_step_reported(self):
        # design 6.3: billing rounds every job up to a minute; a check step that isn't in the
        # Result step's OUTCOMES would fail without failing the job (continue-on-error)
        self.assertEqual(list(REUSABLE["pr-title.yml"]["jobs"]), ["checks"])
        for name in ("pr-title.yml", "security-scan.yml"):
            for job_id, job in REUSABLE[name]["jobs"].items():
                steps = job["steps"]
                checks = [s["id"] for s in steps if s.get("continue-on-error")]
                self.assertTrue(checks, f"{name}: {job_id}")
                result = steps[-1]
                self.assertEqual(result["name"], "Result", f"{name}: {job_id}")
                self.assertNotIn("continue-on-error", result)
                for check in checks:
                    if check == "install":  # licences reports a failed install
                        continue
                    self.assertIn(f"{check}=", result["env"]["OUTCOMES"], f"{name}: {job_id}: {check}")

    def test_pr_title_is_never_cancelled(self):
        # it re-runs on the same commit (edits, assignments); a cancelled run there blocks the PR
        self.assertNotIn("concurrency", REUSABLE["pr-title.yml"]["jobs"]["checks"])

    def test_check_jobs_cancel_superseded_runs(self):
        for name in ("security-scan.yml",):  # runs only on new commits, so a cancelled run is stale
            for job_id, job in REUSABLE[name]["jobs"].items():
                self.assertTrue(job["concurrency"]["cancel-in-progress"], f"{name}: {job_id}")
                self.assertIn("github.ref", job["concurrency"]["group"])

    def test_the_merge_lane_runs_only_mains_code_one_at_a_time(self):
        caller = load("merge-lane-caller.yml")
        self.assertEqual(caller["on"]["pull_request_target"]["types"], ["labeled"])
        # the called workflow gets no more than this; and the lane restarts itself by this file name
        self.assertEqual(caller["permissions"], {"contents": "read", "actions": "write"})
        self.assertIn("merge-lane-caller.yml", str(REUSABLE["merge-lane.yml"]["jobs"]["lane"]["steps"]))
        # an environment secret reaches a called workflow only if the caller passes it (by name)
        self.assertEqual(caller["jobs"]["merge-lane"]["secrets"],
                         {"MERGE_LANE_APP_PRIVATE_KEY": "${{ secrets.MERGE_LANE_APP_PRIVATE_KEY }}"})
        self.assertIn("ready-to-merge", caller["jobs"]["merge-lane"]["if"])
        job = REUSABLE["merge-lane.yml"]["jobs"]["lane"]
        self.assertEqual(job["environment"], "merge-lane")
        self.assertIs(job["concurrency"]["cancel-in-progress"], False)
        for step in job["steps"]:  # never the PR's code: only the scripts from main
            if "checkout" in step.get("uses", ""):
                self.assertEqual(step["with"]["ref"], "main")

if __name__ == "__main__":
    unittest.main()

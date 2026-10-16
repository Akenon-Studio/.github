"""Tests for the reusable workflows every repo calls (design 4.2): their jobs always run, and this
repo calls each one itself. Run: python3 -m unittest discover tests"""

import os
import pathlib
import subprocess
import unittest

import yaml

WORKFLOWS = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows"


def load(name):
    workflow = yaml.safe_load((WORKFLOWS / name).read_text())
    workflow["on"] = workflow.pop(True, workflow.get("on"))  # YAML reads a bare `on:` as True
    return workflow


# The end of a shared check's job name: empty on a PR and on a push to main, so the check keeps its
# required name there, and " (branch)" or " (dispatch)" elsewhere (pr-title.yml).
OFF_A_PR = ("${{ github.event_name == 'workflow_dispatch' && ' (dispatch)' || github.event_name == 'push' "
            "&& github.ref_name != 'main' && ' (branch)' || '' }}")

REUSABLE = {p.name: load(p.name) for p in sorted(WORKFLOWS.glob("*.yml"))
            if "workflow_call" in (load(p.name)["on"] or {})}


class ReusableWorkflowsTest(unittest.TestCase):
    def test_the_shared_checks_are_reusable(self):
        self.assertLessEqual({"pr-title.yml", "issue-fields.yml", "security-scan.yml"}, set(REUSABLE))

    # issue-fields isn't a required check, and its condition reads only the issue, which a PR
    # event doesn't have: it skips Renovate's status pages (design 6.8), never a PR.
    MAY_SKIP = {("issue-fields.yml", "sync")}

    # A job that reads the PR runs on a PR only; elsewhere it is skipped under a name of its own.
    PR_ONLY = {("pr-title.yml", "pr-checks"), ("pr-title.yml", "docs-tick-check")}

    def test_no_job_is_skipped(self):
        # a skipped job reports "skipped", which passes a required check: so none is skipped on a
        # PR, and one skipped off main has a name no PR's check has (no commit of main is a PR's head)
        for name, workflow in REUSABLE.items():
            for job_id, job in workflow["jobs"].items():
                if (name, job_id) in self.PR_ONLY:
                    self.assertEqual(job["if"], "github.event_name == 'pull_request'", f"{name}: {job_id}")
                    self.assertTrue(job["name"].endswith(OFF_A_PR), f"{name}: {job_id}")
                    # an unnamed skip would report the id, so it isn't the check's name either
                    self.assertNotEqual(job_id, job["name"].removesuffix(OFF_A_PR), f"{name}: {job_id}")
                elif (name, job_id) not in self.MAY_SKIP:
                    self.assertNotIn("if", job, f"{name}: job {job_id}")

    def test_runs_off_a_pr_report_names_of_their_own(self):
        # design 7.13 Stage 10: a push to a Peras branch or a dispatch never reports a PR's
        # required name, and a dispatch checks out the commit it names
        for name in ("pr-title.yml", "security-scan.yml"):
            for job_id, job in REUSABLE[name]["jobs"].items():
                self.assertTrue(job.get("name", "").endswith(OFF_A_PR), f"{name}: {job_id}")
                repo = [s for s in job["steps"] if "checkout" in s.get("uses", "")
                        and "repository" not in s.get("with", {})]
                for step in repo if (name, job_id) not in self.PR_ONLY else []:
                    self.assertEqual(step["with"].get("ref"), "${{ github.event.inputs.sha }}", f"{name}: {job_id}")
                if "concurrency" in job:  # a dispatch never cancels main's run, nor another commit's
                    self.assertTrue(job["concurrency"]["group"].endswith("-${{ github.event.inputs.sha }}"))

    def test_issue_fields_skips_exactly_renovates_status_pages(self):
        import json
        bots = json.loads((WORKFLOWS.parent.parent / "rulesets" / "bots.json").read_text())
        condition = REUSABLE["issue-fields.yml"]["jobs"]["sync"]["if"]
        for login in (l for l, s in bots["sources"].items() if s.get("renovate")):
            self.assertIn(f"github.event.issue.user.login == '{login}[bot]'", condition)
        for title in bots["sources"]["renovate"]["status_pages"]:
            self.assertIn(f"github.event.issue.title == '{title}'", condition)
        self.assertEqual(condition.count("github.event.issue.title =="),
                         len(bots["sources"]["renovate"]["status_pages"]))
        self.assertNotIn("pull_request", condition)

    def test_issue_fields_holds_the_key_only_for_members(self):
        # design 6.2: the trust check runs first, holds no secret, and gates every later step
        steps = REUSABLE["issue-fields.yml"]["jobs"]["sync"]["steps"]
        ids = [s.get("id") for s in steps]
        check = ids.index("check")
        self.assertIn("trusted_event.py", steps[check]["run"])
        self.assertNotIn("secrets", str(steps[:check + 1]))
        self.assertLess(check, ids.index("app"))
        for step in steps[check + 1:]:
            self.assertEqual(step.get("if"), "steps.check.outputs.trusted == 'true'", step["name"])

    def test_ai_review_skips_every_renovate(self):
        import json
        bots = json.loads((WORKFLOWS.parent.parent / "rulesets" / "bots.json").read_text())
        wanted = REUSABLE["ai-review.yml"]["jobs"]["review"]["env"]["WANTED"]
        for login in (l for l, s in bots["sources"].items() if s.get("renovate")):
            self.assertIn(f"github.event.pull_request.user.login != '{login}[bot]'", wanted)

    def test_every_renovate_has_the_same_status_pages(self):
        # the issue-fields condition, the failures report and status_page() each read one copy
        import json
        bots = json.loads((WORKFLOWS.parent.parent / "rulesets" / "bots.json").read_text())
        pages = {tuple(s["status_pages"]) for s in bots["sources"].values() if s.get("renovate")}
        self.assertEqual(len(pages), 1)

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
        # each commit of main keeps its own run (Peras reads them); a branch's newer push cancels
        self.assertIn("github.ref == 'refs/heads/main' && github.sha || github.ref",
                      caller["concurrency"]["group"])
        # a copy that adds workflow_dispatch gives each dispatched commit a group of its own
        self.assertTrue(caller["concurrency"]["group"].endswith("-${{ github.event.inputs.sha }}"))


    def test_a_dispatch_runs_only_on_a_peras_branch_and_saves_no_cache(self):
        # its code may be unreviewed (a Peras patch): it never runs in main's cache scope, which
        # every PR reads (Akenon-Studio/.github#206)
        for name in ("pr-title.yml", "security-scan.yml"):
            for job_id, job in REUSABLE[name]["jobs"].items():
                if (name, job_id) in self.PR_ONLY:
                    continue
                first = job["steps"][0]
                self.assertEqual(first["if"], "github.event_name == 'workflow_dispatch'", f"{name}: {job_id}")
                # run it as the runner does (bash -e), with the env it gets
                full = "0123456789abcdef0123456789abcdef01234567"
                for ref, sha, ok in (("refs/heads/peras/12-1", full, True), ("refs/heads/main", full, False),
                                     ("refs/heads/peras-x", full, False), ("refs/heads/peras/12-1", "main", False),
                                     ("refs/heads/peras/12-1", full[:12], False),
                                     ("refs/heads/peras/12-1", full.upper(), False),
                                     ("refs/heads/peras/12-1", full + "\nmain", False),
                                     ("refs/heads/peras/12-1", full + " ", False),
                                     ("refs/heads/peras/12-1", "", False),
                                     ("refs/heads/peras/12-1", "main\n" + full, False),
                                     ("refs/heads/peras/12-1", " " + full, False),
                                     ("refs/heads/peras/12-1", "refs/tags/" + full, False),
                                     ("", full, False)):
                    run = subprocess.run(["bash", "-e", "-c", first["run"]], capture_output=True,
                                         env={"REF": ref, "SHA": sha, "PATH": os.environ["PATH"]})
                    self.assertEqual(run.returncode == 0, ok, f"{name}: {job_id}: {ref} {sha}")
                self.assertEqual(first["env"], {"REF": "${{ github.ref }}", "SHA": "${{ github.event.inputs.sha }}"})
                # and its failure stops the job: nothing after it runs anyway
                self.assertNotIn("continue-on-error", first, f"{name}: {job_id}")
                for step in job["steps"][1:]:
                    # no status function at all (Actions matches names in any case): success() stays implied
                    self.assertNotRegex(str(step.get("if", "")), r"(?i)\b(always|failure|cancelled|success)\s*\(",
                                        f"{name}: {job_id}: {step.get('name')}")
                for step in job["steps"]:
                    cache = step.get("with", {}).get("cache")
                    if cache:
                        self.assertEqual(cache, "${{ github.event_name != 'workflow_dispatch' && 'pnpm' || '' }}",
                                         f"{name}: {job_id}")

    def test_one_job_per_check_workflow_and_every_check_step_reported(self):
        # design 6.3: billing rounds every job up to a minute; a check step that isn't in the
        # Result step's OUTCOMES would fail without failing the job (continue-on-error)
        # one job per thing its checks read (rulesets/required-checks.json, "reads")
        self.assertEqual(list(REUSABLE["pr-title.yml"]["jobs"]), ["pr-checks", "checks", "docs-tick-check"])
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

    def test_checks_reads_only_the_code(self):
        # "pr-title / checks" is marked "code" (rulesets/required-checks.json): a PR's title, body,
        # author or assignees never fail it, so Peras never blames its patch for them (design 7.13
        # Stage 10). Those are "pr-title / pr"'s, which every repo requires (verify-settings.py
        # holds the live rulesets to this file).
        import json
        checks = json.loads((WORKFLOWS.parent.parent / "rulesets" / "required-checks.json").read_text())
        self.assertIn("pr-title / pr", checks["all"])
        self.assertEqual(checks["reads"]["pr-title / checks"], "code")
        job = REUSABLE["pr-title.yml"]["jobs"]["checks"]
        self.assertEqual([s["id"] for s in job["steps"] if s.get("continue-on-error")],
                         ["claude-md", "gaming", "docs"])
        self.assertEqual(job["permissions"], {"contents": "read"})
        for field in ("title", "body", "user", "assignees", "number"):
            self.assertNotIn(f"github.event.pull_request.{field}", str(job), field)

    def test_pr_title_is_never_cancelled(self):
        # it re-runs on the same commit (edits, assignments); a cancelled run there blocks the PR
        for job_id, job in REUSABLE["pr-title.yml"]["jobs"].items():
            self.assertNotIn("concurrency", job, job_id)

    def test_check_jobs_cancel_superseded_runs(self):
        for name in ("security-scan.yml",):  # runs only on new commits, so a cancelled run is stale
            for job_id, job in REUSABLE[name]["jobs"].items():
                self.assertTrue(job["concurrency"]["cancel-in-progress"], f"{name}: {job_id}")
                # a branch's runs share a group; each commit of main has its own (Peras reads them)
                self.assertIn("github.ref == 'refs/heads/main' && github.sha || github.ref",
                              job["concurrency"]["group"], f"{name}: {job_id}")

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

class PrAuthorTest(unittest.TestCase):
    """The required check that refuses an outsider's PR (design 6.2) can't be changed by the PR."""

    def test_it_runs_mains_copy_with_no_secret(self):
        import json
        workflow = load("pr-author.yml")
        self.assertEqual(list(workflow["on"]), ["pull_request_target"])
        steps = workflow["jobs"]["pr-author"]["steps"]
        checkout = steps[0]["with"]
        self.assertEqual(checkout["ref"], "main")
        self.assertIs(checkout["persist-credentials"], False)
        self.assertNotIn("secrets", (WORKFLOWS / "pr-author.yml").read_text())
        self.assertTrue(steps[-1]["run"].endswith("--require"))
        checks = json.loads((WORKFLOWS.parent.parent / "rulesets" / "required-checks.json").read_text())
        self.assertIn("pr-author", checks[".github"])


if __name__ == "__main__":
    unittest.main()

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

    def test_ai_review_takes_only_the_peras_apps_drafts(self):
        # design 6.4 "Peras's PRs" (.github#178)
        wanted = REUSABLE["ai-review.yml"]["jobs"]["review"]["env"]["WANTED"]
        self.assertIn("(!github.event.pull_request.draft ||", wanted)
        import json
        bots = json.loads((WORKFLOWS.parent.parent / "rulesets" / "bots.json").read_text())
        self.assertIn("akenon-studio-peras", bots["sources"])  # the login checked here is a real one
        self.assertIn("github.event.pull_request.user.login == 'akenon-studio-peras[bot]'", wanted)
        self.assertIn("github.event.pull_request.user.type == 'Bot'", wanted)

    def test_ai_review_posts_as_its_app_with_a_key_only_main_can_use(self):
        # design 6.2, 7.12 (.github#225): pull_request_target runs main's workflow, the key is in the
        # main-only `ai-review` environment, and a failed sign-in fails the job
        job = REUSABLE["ai-review.yml"]["jobs"]["review"]
        self.assertEqual(job["environment"], "${{ github.event_name == 'pull_request_target' && 'ai-review' || '' }}")
        self.assertTrue(job["env"]["WANTED"].startswith("${{ github.event_name == 'pull_request_target' &&"))
        self.assertIn("github.event.pull_request.head.repo.full_name == github.repository", job["env"]["WANTED"])
        steps = job["steps"]
        app = next(s for s in steps if s.get("id") == "review_app")
        self.assertNotIn("continue-on-error", app)
        self.assertEqual(app["with"]["permission-pull-requests"], "write")
        self.assertEqual(app["with"]["repositories"], "${{ github.event.repository.name }}")
        self.assertEqual(app["with"]["private-key"], "${{ secrets.AI_REVIEW_APP_PRIVATE_KEY }}")
        review = next(s for s in steps if s.get("name") == "Review")
        self.assertEqual(review["env"]["GITHUB_TOKEN"], "${{ steps.review_app.outputs.token }}")
        self.assertNotIn("github.token", str(steps))
        legacy = next(s for s in steps if s.get("name") == "This caller still runs on pull_request")
        self.assertIn("exit 1", legacy["run"])  # a PR left unreviewed shows red
        self.assertNotIn("pull-requests", REUSABLE["ai-review.yml"]["permissions"])
        # nothing of the PR's runs: its checkout keeps no credentials, and the script is main's
        checkout = next(s for s in steps if s.get("with", {}).get("ref") == "${{ github.event.pull_request.head.sha }}")
        self.assertEqual(checkout["with"]["persist-credentials"], False)
        caller = load("ai-review-caller.yml")
        self.assertEqual(list(caller["on"]), ["pull_request_target"])
        # a round when a PR opens, reopens or is marked ready; a push starts one only on the Peras
        # app's draft, skipped at the caller so no runner starts (design 6.4, Akenon-Studio/handbook#404)
        self.assertEqual(caller["on"]["pull_request_target"]["types"],
                         ["opened", "synchronize", "ready_for_review", "reopened"])
        gate = caller["jobs"]["ai-review"]["if"]
        self.assertIn("github.event.action != 'synchronize'", gate)
        self.assertIn("github.event.pull_request.draft", gate)
        self.assertIn("github.event.pull_request.user.login == 'akenon-studio-peras[bot]'", gate)
        self.assertIn("github.event.pull_request.user.type == 'Bot'", gate)
        self.assertNotIn("pull-requests", caller["permissions"])
        self.assertEqual(caller["jobs"]["ai-review"]["secrets"]["AI_REVIEW_APP_PRIVATE_KEY"],
                         "${{ secrets.AI_REVIEW_APP_PRIVATE_KEY }}")

    def test_the_review_app_is_one_of_the_orgs_apps(self):
        import json
        bots = json.loads((WORKFLOWS.parent.parent / "rulesets" / "bots.json").read_text())
        self.assertIn("akenon-studio-ai-review", bots["org_apps"])

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

    def test_release_decision_is_the_rulesets_and_a_comment_reruns_it(self):
        # design 6.3 (.github#248): the org ruleset runs release-decision.yml from main on PR events
        # only; one job, never skipped or cancelled, main's script, a read token and no secret
        import json
        rulesets = json.loads((WORKFLOWS.parent.parent / "rulesets" / "org-rulesets.json").read_text())
        ruleset = next(r for r in rulesets["rulesets"] if r["name"] == "Release decision")
        self.assertEqual(ruleset["repositories"], ["peras", "platform"])
        self.assertEqual(ruleset["bypass_actors"], [])
        self.assertEqual(ruleset["rules"], [{"type": "workflows", "parameters": {
            "do_not_enforce_on_create": True, "workflows": [
                {"repository": ".github", "path": ".github/workflows/release-decision.yml",
                 "ref": "refs/heads/main"}]}}])
        workflow = load("release-decision.yml")
        self.assertEqual(workflow["on"], {"pull_request": {"types": ["opened", "synchronize", "reopened"]}})
        self.assertEqual(workflow["permissions"], {"contents": "read", "pull-requests": "read"})
        self.assertEqual(list(workflow["jobs"]), ["decision"])
        job = workflow["jobs"]["decision"]
        self.assertNotIn("if", job)
        self.assertNotIn("concurrency", job)
        self.assertNotIn("concurrency", workflow)
        self.assertEqual(job["steps"][0]["with"], {"repository": "akenon-studio/.github", "ref": "main",
                                                   "persist-credentials": False})
        self.assertIn('scripts/release_decision.py judge "$GITHUB_EVENT_PATH"', job["steps"][-1]["run"])
        self.assertNotIn("secrets.", (WORKFLOWS / "release-decision.yml").read_text())
        # the re-run: from main only, never a PR's code, and it reports no check of its own on a head
        rerun = REUSABLE["release-decision-rerun.yml"]
        want = {"contents": "read", "pull-requests": "read", "actions": "write"}
        self.assertEqual(rerun["permissions"], want)
        self.assertEqual(rerun["jobs"]["rerun"]["steps"][0]["with"]["ref"], "main")
        self.assertIn('scripts/release_decision.py rerun "$GITHUB_EVENT_PATH"', rerun["jobs"]["rerun"]["steps"][-1]["run"])
        self.assertNotIn("secrets.", (WORKFLOWS / "release-decision-rerun.yml").read_text())
        caller = load("release-decision-rerun-caller.yml")
        self.assertEqual(caller["permissions"], want)  # a called workflow gets no more than this
        # both run the default branch's caller; never pull_request or a dispatch (a branch's copy)
        self.assertEqual(caller["on"], {"issue_comment": {"types": ["created", "edited", "deleted"]},
                                        "pull_request_target": {"types": ["labeled", "unlabeled"]}})
        self.assertEqual(caller["jobs"]["release-decision"]["if"],
                         "github.event_name != 'issue_comment' || github.event.issue.pull_request")
        # the re-run finds the ruleset's run by the workflow's name and path
        script = (WORKFLOWS.parent.parent / "scripts" / "release_decision.py").read_text()
        self.assertIn(f'WORKFLOW, WORKFLOW_PATH, PR_EVENT = "{workflow["name"]}", '
                      '".github/workflows/release-decision.yml", "pull_request"', script)

class OrgRulesetsTest(unittest.TestCase):
    """rules.desired_org_rulesets(): what apply-rules.py sends GitHub for org-rulesets.json."""

    ARCHIVED = ["ani-motion-animations", "ani-motion-api", "ani-motion-desktop", "ani-motion-pi-client",
                "ani-motion-shared", "ani-motion-web"]
    # the ids GitHub has for them, in that order, and the engineers team's (Merge and PR Gates as live)
    IDS = [1227934210, 1227931808, 1227933553, 1227933890, 1227931468, 1227932798]

    @classmethod
    def fake_gh(cls, archived=True):
        ids = dict(zip(cls.ARCHIVED, cls.IDS))
        def gh(path):
            if path.endswith("/teams/engineers"):
                return {"id": 17359533}
            name = path.rsplit("/", 1)[1]
            return {"id": ids.get(name, 7), "archived": archived and name in ids}
        return gh

    def rules(self):
        import sys
        sys.path.insert(0, str(WORKFLOWS.parent.parent / "scripts"))
        import rules
        return rules

    def test_each_applies_to_its_repos_or_every_managed_repo(self):
        from unittest import mock
        rules = self.rules()
        with mock.patch.object(rules, "gh", self.fake_gh()):
            got = {r["name"]: r for r in rules.desired_org_rulesets()}
        self.assertEqual(got["Action pins"]["conditions"]["repository_name"]["include"], rules.managed_repos())
        self.assertEqual(got["Release decision"]["conditions"]["repository_name"],
                         {"include": ["peras", "platform"], "exclude": []})
        for r in got.values():  # only keys GitHub takes
            self.assertFalse({"repositories", "archived_repositories", "_comment", "_unresolved"} & set(r),
                             r["name"])
            for w in (w for rule in r["rules"] for w in rule.get("parameters", {}).get("workflows", [])):
                self.assertEqual((w["repository_id"], "repository" in w), (7, False))

    def test_merge_and_pr_gates_is_sent_exactly_as_live(self):
        # .github#252: the ruleset made by hand on 2026-08-09, as GET orgs/akenon-studio/rulesets/20604928
        # returned it on 2026-11-03, less what GitHub adds (id, links, dates, source)
        from unittest import mock
        rules = self.rules()
        live = {
            "name": "Merge and PR Gates", "target": "branch", "enforcement": "active", "bypass_actors": [],
            "conditions": {"ref_name": {"exclude": [], "include": ["~DEFAULT_BRANCH", "refs/heads/main",
                                                                  "refs/heads/dev"]},
                           "repository_id": {"repository_ids": self.IDS}},
            "rules": [{"type": "deletion"}, {"type": "non_fast_forward"}, {"type": "pull_request", "parameters": {
                "required_approving_review_count": 1, "dismiss_stale_reviews_on_push": True,
                "required_reviewers": [{"minimum_approvals": 1, "file_patterns": ["*"],
                                        "reviewer": {"id": 17359533, "type": "Team"}}],
                "require_code_owner_review": False, "require_last_push_approval": False,
                "required_review_thread_resolution": False,
                "require_extra_approval_for_unattributed_changes": True, "allowed_merge_methods": ["merge"]}}]}
        with mock.patch.object(rules, "gh", self.fake_gh()):
            got = next(r for r in rules.desired_org_rulesets() if r["name"] == "Merge and PR Gates")
        self.assertEqual(got, live)
        self.assertEqual(rules.org_ruleset_differences(got, {**live, "id": 20604928, "node_id": "x"}), [])

    def test_an_archived_repo_must_be_archived_and_unmanaged(self):
        from unittest import mock
        rules = self.rules()
        with mock.patch.object(rules, "gh", self.fake_gh(archived=False)):
            with self.assertRaises(SystemExit):
                rules.desired_org_rulesets()
        real = rules.load
        def load(name):
            data = real(name)
            if name == "org-rulesets.json":
                data["rulesets"][-1]["archived_repositories"] = ["ani-motion-api", "peras"]
            return data
        with mock.patch.object(rules, "gh", self.fake_gh()), mock.patch.object(rules, "load", load):
            with self.assertRaises(SystemExit):
                rules.desired_org_rulesets()

    def test_a_reviewer_team_must_be_in_teams_json(self):
        from unittest import mock
        rules = self.rules()
        real = rules.load
        def load(name):
            data = real(name)
            if name == "org-rulesets.json":
                pr = data["rulesets"][-1]["rules"][-1]["parameters"]
                pr["required_reviewers"][0]["reviewer"]["team"] = "nobody"
            return data
        with mock.patch.object(rules, "gh", self.fake_gh()), mock.patch.object(rules, "load", load):
            with self.assertRaises(SystemExit):
                rules.desired_org_rulesets()

    def test_an_unreadable_name_marks_only_its_ruleset(self):
        # verify-settings.py reads with try_gh: what its token can't read skips that ruleset, with a note
        rules = self.rules()
        gh = self.fake_gh()
        got = {r["name"]: r for r in rules.desired_org_rulesets(
            lookup=lambda path: None if path.endswith("/teams/engineers") else gh(path))}
        self.assertEqual(got["Merge and PR Gates"]["_unresolved"], "team engineers")
        self.assertNotIn("_unresolved", got["Action pins"])

    def test_differences_are_found_both_ways(self):
        rules = self.rules()
        want = {"name": "x", "conditions": {"ref_name": {"include": ["a"], "exclude": []}},
                "rules": [{"type": "pull_request", "parameters": {"allowed_merge_methods": ["merge"]}}]}
        live = {"name": "x", "id": 1,
                "conditions": {"ref_name": {"include": ["a"], "exclude": []},
                               "repository_name": {"include": ["y"], "exclude": []}},
                "rules": [{"type": "pull_request", "parameters": {"allowed_merge_methods": ["squash"],
                                                                  "dismiss_stale_reviews_on_push": True}},
                          {"type": "workflows", "parameters": {"workflows": [
                              {"repository_id": 1, "path": "p", "ref": "r", "sha": "s"}]}}]}
        diffs = rules.org_ruleset_differences(want, live)
        self.assertTrue(any("allowed_merge_methods" in d for d in diffs), diffs)
        self.assertTrue(any("live has more" in d and "repository_name" in d for d in diffs), diffs)
        self.assertTrue(any("live has more" in d and "dismiss_stale" in d for d in diffs), diffs)
        self.assertTrue(any("workflows" in d for d in diffs), diffs)
        self.assertEqual(rules.org_ruleset_differences(want, {**want, "id": 1, "_links": {}}), [])

    def test_github_defaults_are_no_drift(self):
        # a key GitHub fills in with its default (false, 0, empty) that the file doesn't set
        rules = self.rules()
        want = {"conditions": {"repository_name": {"include": ["y"], "exclude": []}},
                "rules": [{"type": "pull_request", "parameters": {"allowed_merge_methods": ["merge"]}}]}
        live = {"conditions": {"repository_name": {"include": ["y"], "exclude": [], "protected": False}},
                "rules": [{"type": "pull_request", "parameters": {"allowed_merge_methods": ["merge"],
                                                                  "require_code_owner_review": False,
                                                                  "required_approving_review_count": 0}}]}
        self.assertEqual(rules.org_ruleset_differences(want, live), [])
        # a default the file sets inside a list compared whole matches live's too
        want["bypass_actors"] = [{"actor_id": None, "actor_type": "OrganizationAdmin", "bypass_mode": "always"}]
        live["bypass_actors"] = [dict(want["bypass_actors"][0])]
        self.assertEqual(rules.org_ruleset_differences(want, live), [])

    def test_a_missing_or_extra_rule_is_named_once_the_right_way_round(self):
        rules = self.rules()
        want = {"rules": [{"type": "deletion"}, {"type": "non_fast_forward"}]}
        live = {"rules": [{"type": "deletion"}, {"type": "creation"}]}
        self.assertEqual(rules.org_ruleset_differences(want, live),
                         ["/rules[non_fast_forward]: expected an object, got None",
                          "/rules: unexpected rule 'creation'"])

    def test_the_daily_check_reports_an_unarchived_repo_and_goes_on(self):
        rules = self.rules()
        got = {r["name"]: r for r in rules.desired_org_rulesets(lookup=self.fake_gh(archived=False))}
        self.assertIn("as archived, but it is not", got["Merge and PR Gates"]["_problem"])
        self.assertNotIn("_problem", got["Action pins"])

    def test_the_plan_changes_nothing_and_counts_what_it_would(self):
        import contextlib, importlib.util, io
        from unittest import mock
        self.rules()
        spec = importlib.util.spec_from_file_location(
            "apply_rules", WORKFLOWS.parent.parent / "scripts" / "apply-rules.py")
        apply_rules = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(apply_rules)
        def gh(*args, body=None):
            raise AssertionError(f"the plan called gh {args}")
        same = {"name": "same", "rules": [{"type": "deletion"}]}
        wanted = [same, {"name": "changed", "rules": [{"type": "deletion"}]}, {"name": "new", "rules": []}]
        live = {"same": {**same, "id": 1}, "changed": {"name": "changed", "rules": []},
                "extra": {"name": "extra", "rules": []}}
        out = io.StringIO()
        with mock.patch.object(apply_rules, "gh", gh), \
                mock.patch.object(apply_rules, "org_rulesets", lambda: live), \
                mock.patch.object(apply_rules, "desired_org_rulesets", lambda: wanted), \
                contextlib.redirect_stdout(out):
            self.assertEqual(apply_rules.plan_org_rulesets(), 2)
        text = out.getvalue()
        self.assertIn("'same' unchanged", text)
        self.assertIn("'changed' would be updated:\n  /rules[deletion]", text)
        self.assertIn("'new' would be created", text)
        self.assertIn("'extra' is not in rulesets/org-rulesets.json; left alone", text)

    def test_a_repo_not_in_repos_txt_is_refused(self):
        from unittest import mock
        rules = self.rules()
        real = rules.load
        def load(name):
            data = real(name)
            if name == "org-rulesets.json":
                data["rulesets"][1]["repositories"] = ["peras", "nope"]
            return data
        with mock.patch.object(rules, "gh", self.fake_gh()), mock.patch.object(rules, "load", load):
            with self.assertRaises(SystemExit):
                rules.desired_org_rulesets()


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


class SettingsCheckLogTest(unittest.TestCase):
    """The settings check's public log says only whether each step ran, never what it found
    (design 6.2)."""

    def setUp(self):
        self.steps = load("settings-check.yml")["jobs"]["check"]["steps"]

    def test_no_printed_command_carries_the_verify_code(self):
        # GitHub prints a step's command with its expressions filled in, and its env
        for step in self.steps:
            self.assertNotIn("steps.verify", step.get("run", "") + str(step.get("env", "")), step.get("name"))
        verify = next(s for s in self.steps if s.get("id") == "verify")
        self.assertIn('echo "$?" > verify.code', verify["run"])
        self.assertNotIn("GITHUB_OUTPUT", verify["run"])
        report = next(s for s in self.steps if "report_drift.py" in s.get("run", ""))
        self.assertEqual(report["run"],
                         "python3 scripts/quiet_run.py python3 scripts/report_drift.py verify.txt verify.code")


if __name__ == "__main__":
    unittest.main()

"""Tests that every check rulesets/required-checks.json requires of all repos, or of this one, is a
job a workflow here produces (a required check that never runs blocks every merge), and that every
repo carries the caller of each reusable workflow whose checks are required everywhere, and that
every required check is marked with what it reads (design 7.13 Stage 10).
Run: python3 -m unittest discover tests"""

import json
import pathlib
import re
import sys
import unittest

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
from rules import reads_problems  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
CHECKS = json.loads((ROOT / "rulesets" / "required-checks.json").read_text())
REQUIRED_FILES = json.loads((ROOT / "rulesets" / "repo-lists.json").read_text())["required_files"]


def workflows():
    out = {}
    for path in sorted(WORKFLOWS.glob("*.yml")):
        workflow = yaml.safe_load(path.read_text())
        workflow["on"] = workflow.pop(True, workflow.get("on"))  # YAML reads a bare `on:` as True
        out[path.name] = workflow
    return out


def static(name):
    """A job name as a PR's run reports it: a reusable workflow's names end in an expression that is
    empty on a PR (pr-title.yml, "Runs that aren't a PR's")."""
    return re.sub(r"\$\{\{.*?\}\}", "", name)


def produced_checks():
    """Check names this repo's workflows report: a plain job's id or name, and '<caller job> /
    <job>' for a job that calls a reusable workflow here."""
    flows, names = workflows(), set()
    for workflow in flows.values():
        for job_id, job in (workflow.get("jobs") or {}).items():
            uses = job.get("uses", "")
            if uses.startswith("./.github/workflows/"):
                called = flows[uses.rsplit("/", 1)[1]]
                names |= {f"{job.get('name', job_id)} / {static(j.get('name', i))}"
                          for i, j in called["jobs"].items()}
            elif "workflow_call" not in (workflow["on"] or {}):
                names.add(static(job.get("name", job_id)))
    return names


class RequiredChecksTest(unittest.TestCase):
    def test_checks_for_every_repo_and_this_one_are_produced_here(self):
        produced = produced_checks()
        for name in CHECKS["all"] + CHECKS[".github"]:
            self.assertIn(name, produced)

    def test_every_required_check_is_marked_with_what_it_reads(self):
        self.assertEqual(reads_problems(CHECKS), [])

    def test_peras_requires_its_looks_bump_check_read_as_pr(self):
        # .github#246, peras#608: a render host or runtime bump merges only once its before/after
        # shows its head. peras's ci.yml reports the job as plain `looks-bump` on every PR; it reads
        # the PR's comments, so a failure pauses Peras rather than patching code (design 7.13 Stage 10)
        self.assertIn("looks-bump", CHECKS["peras"])
        self.assertEqual(CHECKS["reads"]["looks-bump"], "pr")

    def test_every_repo_carries_the_callers_of_checks_required_everywhere(self):
        for name in CHECKS["all"]:
            caller = f".github/workflows/{name.split(' / ')[0]}-caller.yml"
            self.assertIn(caller, REQUIRED_FILES, name)
            self.assertTrue((ROOT / caller).is_file(), caller)


class ReadsProblemsTest(unittest.TestCase):
    def test_a_fully_marked_file_passes(self):
        checks = {"_comment": "x", "all": ["a"], "web": ["b"], "reads": {"a": "pr", "b": "code"}}
        self.assertEqual(reads_problems(checks), [])

    def test_an_unmarked_required_check_fails(self):
        problems = reads_problems({"all": ["a"], "web": ["b"], "reads": {"a": "pr"}})
        self.assertEqual(problems, ["required check 'b' has no 'reads' mark"])

    def test_no_reads_at_all_fails_for_every_check(self):
        self.assertEqual(len(reads_problems({"all": ["a", "b"]})), 2)

    def test_an_unknown_mark_fails(self):
        problems = reads_problems({"all": ["a"], "reads": {"a": "tests"}})
        self.assertEqual(len(problems), 1)
        self.assertIn("'tests'", problems[0])

    def test_a_mark_for_a_check_no_repo_requires_fails(self):
        problems = reads_problems({"all": ["a"], "reads": {"a": "code", "gone": "code"}})
        self.assertEqual(problems, ["'reads' marks 'gone', which no repo requires"])

    def test_every_mark_peras_reads_is_allowed(self):
        # peras/core/orchestrator/github.ts requiredChecks: 'pr', 'diff', 'person', else 'code'
        for mark in ("pr", "diff", "code", "person"):
            self.assertEqual(reads_problems({"all": ["a"], "reads": {"a": mark}}), [])


if __name__ == "__main__":
    unittest.main()

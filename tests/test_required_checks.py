"""Tests that every check rulesets/required-checks.json requires of all repos, or of this one, is a
job a workflow here produces (a required check that never runs blocks every merge), and that every
repo carries the caller of each reusable workflow whose checks are required everywhere.
Run: python3 -m unittest discover tests"""

import json
import pathlib
import unittest

import yaml

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


def produced_checks():
    """Check names this repo's workflows report: a plain job's id or name, and '<caller job> /
    <job>' for a job that calls a reusable workflow here."""
    flows, names = workflows(), set()
    for workflow in flows.values():
        for job_id, job in (workflow.get("jobs") or {}).items():
            uses = job.get("uses", "")
            if uses.startswith("./.github/workflows/"):
                called = flows[uses.rsplit("/", 1)[1]]
                names |= {f"{job.get('name', job_id)} / {j.get('name', i)}"
                          for i, j in called["jobs"].items()}
            elif "workflow_call" not in (workflow["on"] or {}):
                names.add(job.get("name", job_id))
    return names


class RequiredChecksTest(unittest.TestCase):
    def test_checks_for_every_repo_and_this_one_are_produced_here(self):
        produced = produced_checks()
        for name in CHECKS["all"] + CHECKS[".github"]:
            self.assertIn(name, produced)

    def test_every_repo_carries_the_callers_of_checks_required_everywhere(self):
        for name in CHECKS["all"]:
            caller = f".github/workflows/{name.split(' / ')[0]}-caller.yml"
            self.assertIn(caller, REQUIRED_FILES, name)
            self.assertTrue((ROOT / caller).is_file(), caller)


if __name__ == "__main__":
    unittest.main()

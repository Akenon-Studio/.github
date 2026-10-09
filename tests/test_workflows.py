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

    def test_no_job_is_skipped(self):
        # a skipped job reports "skipped", which passes a required check
        for name, workflow in REUSABLE.items():
            for job_id, job in workflow["jobs"].items():
                self.assertNotIn("if", job, f"{name}: job {job_id}")

    def test_this_repo_calls_each_one_from_its_own_copy(self):
        for name in REUSABLE:
            caller = load(name.replace(".yml", "-caller.yml"))
            uses = [j.get("uses") for j in caller["jobs"].values()]
            self.assertEqual(uses, [f"./.github/workflows/{name}"], name)

    def test_security_scan_checks(self):
        caller = load("security-scan-caller.yml")
        self.assertEqual(list(caller["jobs"]), ["security-scan"])
        self.assertEqual(sorted(REUSABLE["security-scan.yml"]["jobs"]), ["code", "dependencies", "secrets"])
        self.assertEqual(set(caller["on"]), {"pull_request", "push"})


if __name__ == "__main__":
    unittest.main()

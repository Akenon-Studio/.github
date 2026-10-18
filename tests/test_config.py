"""Tests that rulesets/ agrees with the issue forms, and for the label and issue type checks in
scripts/rules.py. Run: python3 -m unittest discover tests"""

import json
import pathlib
import shlex
import sys
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from rules import desired_labels, environment_differences, issue_type_differences, label_differences, load  # noqa: E402

FORMS = [yaml.safe_load(p.read_text()) for p in sorted((ROOT / ".github" / "ISSUE_TEMPLATE").glob("*.yml"))
         if p.name != "config.yml"]
TYPES = load("issue-types.json")["types"]
BOARD = json.loads((ROOT / "rulesets" / "board.json").read_text())


class FormsMatchConfigTest(unittest.TestCase):
    def test_every_form_type_is_an_enabled_issue_type(self):
        enabled = {t["name"] for t in TYPES if t["is_enabled"]}
        for form in FORMS:
            self.assertIn(form["type"], enabled, form["name"])

    def test_every_form_label_exists_in_every_repo(self):
        everywhere = {l["name"] for l in load("labels.json")["all"]}
        for form in FORMS:
            for label in form.get("labels", []):
                self.assertIn(label, everywhere, form["name"])

    def test_automation_labels_are_defined(self):
        self.assertIn("needs-fields", {l["name"] for l in desired_labels("platform")})

    def test_label_descriptions_fit_githubs_limit(self):
        # GitHub refuses a label description over 100 characters (.github#138)
        labels = json.loads((ROOT / "rulesets" / "labels.json").read_text())
        for group in (v for v in labels.values() if isinstance(v, list)):
            for label in group:
                self.assertLessEqual(len(label["description"]), 100, label["name"])
        self.assertIn("confirmed", {l["name"] for l in desired_labels("platform")})
        self.assertIn("settings-drift", {l["name"] for l in desired_labels(".github-private")})
        self.assertNotIn("settings-drift", {l["name"] for l in desired_labels(".github")})  # public
        self.assertNotIn("settings-drift", {l["name"] for l in desired_labels("platform")})

    def test_every_view_type_filter_names_an_issue_type(self):
        names = {t["name"] for t in TYPES}
        for view in BOARD["views"]:
            for part in shlex.split(view["filter"]):  # drops quotes: type:Audit finding,Decision
                if part.startswith("type:"):
                    for name in part[5:].split(","):
                        self.assertIn(name, names, view["name"])


LABEL = {"name": "task", "color": "ededed", "description": "Task form"}


class LabelDifferencesTest(unittest.TestCase):
    def test_same_label_passes(self):
        self.assertEqual(label_differences([LABEL], [dict(LABEL, name="Task", color="EDEDED")]), [])

    def test_missing_label(self):
        self.assertEqual(label_differences([LABEL], []), ["label 'task' is missing"])

    def test_wrong_description(self):
        self.assertEqual(len(label_differences([LABEL], [dict(LABEL, description=None)])), 1)

    def test_extra_live_labels_are_ignored(self):
        self.assertEqual(label_differences([LABEL], [LABEL, dict(LABEL, name="wontfix")]), [])


TYPE = {"name": "Process failure", "description": "x", "color": "pink", "is_enabled": True}


class IssueTypeDifferencesTest(unittest.TestCase):
    def test_same_type_passes(self):
        self.assertEqual(issue_type_differences([TYPE], [dict(TYPE, id=1)]), [])

    def test_missing_type(self):
        self.assertEqual(issue_type_differences([TYPE], []), ["issue type 'Process failure' is missing"])

    def test_disabled_type(self):
        self.assertEqual(len(issue_type_differences([TYPE], [dict(TYPE, is_enabled=False)])), 1)

    def test_unexpected_live_type(self):
        self.assertEqual(issue_type_differences([TYPE], [TYPE, dict(TYPE, name="Epic")]),
                         ["issue type 'Epic' is not in issue-types.json"])


class EnvironmentsTest(unittest.TestCase):
    """rulesets/environments.json against an environment as GitHub has it (design 6.2, .github#225)."""
    WANT = {"when": "x", "branches": ["main"], "secrets": ["KEY"]}
    GOOD = {"deployment_branch_policy": {"custom_branch_policies": True, "protected_branches": False}}

    def test_main_only_with_its_key_passes(self):
        self.assertEqual(environment_differences("e", self.GOOD, [{"name": "main", "type": "branch"}], ["KEY"], self.WANT), [])

    def test_missing_loose_or_keyless_is_reported(self):
        self.assertEqual(environment_differences("e", None, [], [], self.WANT), ["environment 'e' is missing"])
        anyone = {"deployment_branch_policy": None}
        self.assertIn("environment 'e' isn't limited to chosen branches (want main)",
                      environment_differences("e", anyone, [], ["KEY"], self.WANT))
        wider = environment_differences("e", self.GOOD, [{"name": "main", "type": "branch"}, {"name": "feat/*", "type": "branch"}], ["KEY"], self.WANT)
        self.assertEqual(wider, ["environment 'e' deploys from ['feat/*', 'main'], want ['main']"])
        tagged = environment_differences("e", self.GOOD, [{"name": "main", "type": "branch"}, {"name": "v*", "type": "tag"}], ["KEY"], self.WANT)
        self.assertEqual(tagged, ["environment 'e' deploys from ['main', 'tag v*'], want ['main']"])
        self.assertEqual(environment_differences("e", self.GOOD, [{"name": "main", "type": "branch"}], [], self.WANT),
                         ["environment 'e' has no secret KEY"])

    def test_every_environment_is_main_only(self):
        for name, want in load("environments.json")["environments"].items():
            self.assertEqual(want["branches"], ["main"], name)
            self.assertTrue((ROOT / want["when"]).exists(), name)  # this repo's caller names it


if __name__ == "__main__":
    unittest.main()

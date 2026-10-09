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

from rules import desired_labels, issue_type_differences, label_differences, load  # noqa: E402

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
        self.assertIn("confirmed", {l["name"] for l in desired_labels("platform")})
        self.assertIn("settings-drift", {l["name"] for l in desired_labels(".github")})
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


if __name__ == "__main__":
    unittest.main()

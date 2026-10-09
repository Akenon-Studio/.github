"""Tests for the teams in rulesets/teams.json and their checks in scripts/rules.py.
Run: python3 -m unittest discover tests"""

import pathlib
import re
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from rules import (AUTOMATION_OWNERS, automation_owners, load, managed_repos,  # noqa: E402
                   team, team_differences, team_repo_permission)

OWNERS = team(AUTOMATION_OWNERS)
LIVE = {"name": OWNERS["name"], "slug": OWNERS["slug"], "description": OWNERS["description"],
        "privacy": OWNERS["privacy"], "notification_setting": "notifications_enabled"}
ALL_REPOS = {r: "pull" for r in managed_repos()}


class TeamsFileTest(unittest.TestCase):
    def test_automation_owners_has_someone(self):
        self.assertEqual(automation_owners(), ["RuvinduH"])

    def test_each_slug_is_what_github_makes_from_the_name(self):
        for t in load("teams.json")["teams"]:
            self.assertEqual(re.sub(r"[^a-z0-9]+", "-", t["name"].lower()).strip("-"), t["slug"])

    def test_each_team_names_a_real_permission(self):
        for t in load("teams.json")["teams"]:
            self.assertIn(t["repo_permission"], {"pull", "triage", "push", "maintain", "admin"})


class TeamDifferencesTest(unittest.TestCase):
    def test_matching_team_passes(self):
        self.assertEqual(team_differences(OWNERS, LIVE, ["ruvinduh"], ALL_REPOS), [])

    def test_missing_team(self):
        self.assertEqual(team_differences(OWNERS, None, [], {}),
                         ["team 'automation-owners' is missing"])

    def test_missing_and_extra_members(self):
        found = team_differences(OWNERS, LIVE, ["Thytus777"], ALL_REPOS)
        self.assertEqual(len(found), 2)
        self.assertIn("RuvinduH is not a member", found[0])
        self.assertIn("Thytus777 is a member but not in teams.json", found[1])

    def test_wrong_description(self):
        found = team_differences(OWNERS, {**LIVE, "description": "x"}, ["RuvinduH"], ALL_REPOS)
        self.assertEqual(len(found), 1)
        self.assertIn("description", found[0])

    def test_repo_without_access(self):
        repos = {**ALL_REPOS}
        repos.pop("platform")
        found = team_differences(OWNERS, LIVE, ["RuvinduH"], repos)
        self.assertEqual(found, ["team 'automation-owners': expected 'pull' on platform, got None"])


class PermissionTest(unittest.TestCase):
    def test_highest_flag_wins(self):
        self.assertEqual(team_repo_permission({"pull": True, "triage": False, "push": False,
                                               "maintain": False, "admin": False}), "pull")
        self.assertEqual(team_repo_permission({"pull": True, "push": True}), "push")
        self.assertIsNone(team_repo_permission({}))


if __name__ == "__main__":
    unittest.main()

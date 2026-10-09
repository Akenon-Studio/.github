"""Tests for the teams in rulesets/teams.json and their checks in scripts/rules.py.
Run: python3 -m unittest discover tests"""

import pathlib
import re
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from rules import (AUTOMATION_OWNERS, ROOT, automation_owners, codeowners_team_differences,  # noqa: E402
                   codeowners_teams, load, managed_repos, team, team_differences,
                   org_role_differences, team_repo_permission, team_repos)

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

    def test_engineers_can_write_everywhere(self):
        engineers = team("engineers")
        self.assertEqual(engineers["members"], ["Thytus777", "RuvinduH"])
        self.assertEqual(engineers["repo_permission"], "push")

    def test_this_repos_codeowners_teams_are_defined_with_write(self):
        text = (ROOT / ".github" / "CODEOWNERS").read_text()
        self.assertEqual(codeowners_team_differences(text, load("teams.json")["teams"]), [])

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


class CodeownersTeamsTest(unittest.TestCase):
    TEAMS = [{"slug": "engineers", "repo_permission": "push"},
             {"slug": "automation-owners", "repo_permission": "pull"}]

    def test_reads_this_orgs_teams_only(self):
        text = ("# a comment naming @akenon-studio/ghosts\n"
                "* @Akenon-Studio/Engineers @someone\n"
                "/docs/ @akenon-studio/engineers @other-org/team  # trailing @akenon-studio/x\n"
                "/ci/ @akenon-studio/design dev@example.com\n")
        self.assertEqual(codeowners_teams(text), ["engineers", "design"])

    def test_defined_team_with_write_passes(self):
        self.assertEqual(codeowners_team_differences("* @akenon-studio/engineers\n", self.TEAMS), [])

    def test_team_not_in_teams_json(self):
        self.assertEqual(codeowners_team_differences("* @akenon-studio/design\n", self.TEAMS),
                         ["CODEOWNERS names team 'design', which is not in rulesets/teams.json"])

    def test_team_without_write(self):
        found = codeowners_team_differences("* @akenon-studio/automation-owners\n", self.TEAMS)
        self.assertEqual(len(found), 1)
        self.assertIn("'pull'; a code owner needs write", found[0])

    def test_live_team_without_write_is_a_difference(self):
        engineers = team("engineers")
        live = {k: engineers[k] for k in ("name", "slug", "description", "privacy")}
        repos = {r: "push" for r in managed_repos()}
        self.assertEqual(team_differences(engineers, live, ["thytus777", "ruvinduh"], repos), [])
        repos["peras"] = "pull"
        self.assertEqual(team_differences(engineers, live, ["Thytus777", "RuvinduH"], repos),
                         ["team 'engineers': expected 'push' on peras, got 'pull'"])


class PermissionTest(unittest.TestCase):
    def test_highest_flag_wins(self):
        self.assertEqual(team_repo_permission({"pull": True, "triage": False, "push": False,
                                               "maintain": False, "admin": False}), "pull")
        self.assertEqual(team_repo_permission({"pull": True, "push": True}), "push")
        self.assertIsNone(team_repo_permission({}))


if __name__ == "__main__":
    unittest.main()


DESIGN_TEAM = team("design")


class LimitedTeamTest(unittest.TestCase):
    """A team with a `repos` list (handbook#103): access there, none elsewhere."""
    DESIGN = DESIGN_TEAM
    LIVE = {k: DESIGN_TEAM[k] for k in ("name", "slug", "description", "privacy")}

    def test_design_writes_to_handbook_and_hardware_only(self):
        self.assertEqual(sorted(team_repos(self.DESIGN)), ["handbook", "hardware"])
        self.assertEqual(team_differences(self.DESIGN, self.LIVE,
                                          ["winterleaf354-jpg", "InhibitedHail91"],
                                          {"handbook": "push", "hardware": "push"}), [])

    def test_access_outside_its_repos_is_a_difference(self):
        found = team_differences(self.DESIGN, self.LIVE, ["winterleaf354-jpg", "InhibitedHail91"],
                                 {"handbook": "push", "hardware": "push", "platform": "pull"})
        self.assertEqual(found, ["team 'design': expected None on platform, got 'pull'"])

    def test_codeowner_outside_its_repos(self):
        teams = [{"slug": "design", "repo_permission": "push", "repos": ["handbook"]}]
        text = "* @akenon-studio/design\n"
        self.assertEqual(codeowners_team_differences(text, teams, "handbook"), [])
        found = codeowners_team_differences(text, teams, "platform")
        self.assertEqual(len(found), 1)
        self.assertIn("no access to platform", found[0])


class OrgRoleTest(unittest.TestCase):
    """handbook#107: org roles given to teams match teams.json."""
    TEAMS = [{"slug": "engineers"}, {"slug": "design", "org_role": "all_repo_read"}]

    def test_matching_roles_pass(self):
        self.assertEqual(org_role_differences(self.TEAMS, {"design": {"all_repo_read"}}), [])

    def test_extra_role_is_a_difference(self):
        found = org_role_differences(self.TEAMS, {"engineers": {"all_repo_admin"},
                                                  "design": {"all_repo_read"}})
        self.assertEqual(found, ["team 'engineers': expected org role none, got ['all_repo_admin']"])

    def test_missing_role_is_a_difference(self):
        self.assertEqual(len(org_role_differences(self.TEAMS, {})), 1)

    def test_teams_json_gives_design_read_and_engineers_none(self):
        self.assertEqual(team("design").get("org_role"), "all_repo_read")
        self.assertIsNone(team("engineers").get("org_role"))

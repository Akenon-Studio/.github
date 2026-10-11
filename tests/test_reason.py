"""Tests for scripts/reason.py: the board's Reason field, and which bot output it flags.
Run: python3 -m unittest discover tests"""

import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import reason  # noqa: E402

RULE = json.loads((ROOT / "rulesets" / "board.json").read_text())["reasons"]
PERSON = {"__typename": "User", "login": "someone"}
AUTOMATION = {"__typename": "Bot", "login": "akenon-studio-automation"}
OTHER_BOT = {"__typename": "Bot", "login": "someapp"}


def why(issue_type="Task", labels=(), status="Todo", author=PERSON):
    return reason.issue_reason(issue_type, labels, status, author, RULE)


class OptionsTest(unittest.TestCase):
    def test_the_names_are_the_board_fields_options_in_order(self):
        board = json.loads((ROOT / "rulesets" / "board.json").read_text())
        field = next(f for f in board["fields"] if f["name"] == reason.FIELD)
        self.assertEqual([o["name"] for o in field["options"]],
                         [reason.NEEDS_FIELDS, reason.REVIEW, reason.REPORT, reason.NEW_BUG,
                          reason.WAITING_ON])

    def test_needs_a_human_groups_by_it(self):
        board = json.loads((ROOT / "rulesets" / "board.json").read_text())
        view = next(v for v in board["views"] if v["name"] == "Needs a human")
        self.assertEqual(view["group_by"], reason.FIELD)
        self.assertIn("has:reason", view["filter"])
        self.assertNotIn("Needs fixing", [v["name"] for v in board["views"]])


class IssueReasonTest(unittest.TestCase):
    def test_each_reason(self):
        self.assertEqual(why(labels=["needs-fields"]), reason.NEEDS_FIELDS)
        self.assertEqual(why(status="In review"), reason.REVIEW)
        self.assertEqual(why(author=AUTOMATION, labels=["ai-scorecard"]), reason.REPORT)
        self.assertEqual(why("Bug", ["bug", "triage"], "Waiting for human"), reason.NEW_BUG)
        self.assertEqual(why(status="Waiting for human"), reason.WAITING_ON)

    def test_nothing_applies(self):
        self.assertIsNone(why())
        self.assertIsNone(why(status="In progress"))
        self.assertIsNone(why("Bug", ["bug"], "In progress"))  # triaged

    def test_the_first_that_applies_wins(self):
        self.assertEqual(why("Bug", ["needs-fields", "triage"], "In review"), reason.NEEDS_FIELDS)
        self.assertEqual(why(status="In review", author=AUTOMATION), reason.REVIEW)
        self.assertEqual(why(status="Waiting for human", author=AUTOMATION), reason.REPORT)
        self.assertEqual(why("Bug", ["triage"], "Waiting for human"), reason.NEW_BUG)


class IssueProblemsTest(unittest.TestCase):
    def problems(self, author, labels=(), status="Todo"):
        return reason.issue_problems("Task", labels, status, author, RULE)

    def test_a_report_and_a_persons_issue_are_fine(self):
        self.assertEqual(self.problems(AUTOMATION), [])
        self.assertEqual(self.problems(PERSON), [])

    def test_a_bot_no_rule_covers_is_flagged(self):
        self.assertEqual(len(self.problems(OTHER_BOT)), 1)

    def test_its_own_flag_doesnt_count_as_a_reason(self):
        self.assertEqual(len(self.problems(OTHER_BOT, ["needs-fields"])), 1)

    def test_a_reason_from_its_status_or_an_exempt_label_is_enough(self):
        self.assertEqual(self.problems(OTHER_BOT, status="Waiting for human"), [])
        self.assertEqual(self.problems(OTHER_BOT, ["peras"]), [])


def pr(login="renovate", labels=(), closes=0, author="Bot"):
    return {"author": {"__typename": author, "login": login},
            "labels": {"nodes": [{"name": l} for l in labels]},
            "closingIssuesReferences": {"totalCount": closes}}


class PrTest(unittest.TestCase):
    SOURCES = {"renovate": {"labels": ["dependencies"], "no_issue": True},
               "akenon-studio-automation": {"labels": ["settings-drift"], "no_issue": False}}

    def test_a_bot_pr_closing_no_issue_goes_on_the_board(self):
        self.assertTrue(reason.pr_on_board(pr(), RULE))

    def test_others_stay_off(self):
        self.assertFalse(reason.pr_on_board(pr(closes=1), RULE))  # its issue carries it
        self.assertFalse(reason.pr_on_board(pr(author="User"), RULE))
        self.assertFalse(reason.pr_on_board(pr(labels=["peras"]), RULE))

    def test_only_a_no_issue_bot_may_close_nothing(self):
        self.assertEqual(reason.pr_problems(pr(), self.SOURCES), [])
        self.assertEqual(len(reason.pr_problems(pr("akenon-studio-automation"), self.SOURCES)), 1)
        self.assertEqual(len(reason.pr_problems(pr("someapp[bot]"), self.SOURCES)), 1)

    def test_its_reason(self):
        self.assertEqual(reason.pr_reason({"dependencies"}), reason.REVIEW)
        self.assertEqual(reason.pr_reason({"needs-fields"}), reason.NEEDS_FIELDS)


if __name__ == "__main__":
    unittest.main()

"""Tests for scripts/pr_status.py. Run: python3 -m unittest discover tests"""

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

import pr_status  # noqa: E402
from pr_status import new_status, wanted  # noqa: E402

REPO = "Akenon-Studio/platform"


def event(action, draft=False, merged=False, head=REPO, login="someone", user_type="User"):
    return {"action": action, "repository": {"full_name": REPO},
            "pull_request": {"number": 7, "draft": draft, "merged": merged, "head": {"repo": {"full_name": head}},
                             "user": {"login": login, "type": user_type}}}


class DecideTest(unittest.TestCase):
    def test_a_ready_pr_puts_its_issues_in_review(self):
        for action in ("opened", "reopened", "edited", "ready_for_review"):
            self.assertEqual(wanted(event(action)), "review", action)
        self.assertIsNone(wanted(event("opened", draft=True)))

    def test_a_draft_or_an_unmerged_close_takes_them_back(self):
        self.assertEqual(wanted(event("converted_to_draft", draft=True)), "back")
        self.assertEqual(wanted(event("closed")), "back")
        self.assertIsNone(wanted(event("closed", merged=True)))  # the merge closes them: Done

    def test_a_fork_changes_nothing(self):
        self.assertIsNone(wanted(event("opened", head="someone/platform")))
        self.assertIsNone(wanted({**event("opened"), "pull_request": {
            **event("opened")["pull_request"], "head": {"repo": None}}}))  # a deleted fork

    def test_the_status_to_set(self):
        self.assertEqual(new_status("review", "Todo", False), "In review")
        self.assertEqual(new_status("review", "Waiting for human", True), "In review")
        self.assertEqual(new_status("review", None, True), "In review")  # just added to the board
        self.assertIsNone(new_status("review", "In review", True))
        self.assertIsNone(new_status("review", "Done", True))
        self.assertEqual(new_status("back", "In review", True), "In progress")
        self.assertEqual(new_status("back", "In review", False), "Todo")
        self.assertIsNone(new_status("back", "Blocked", True))  # set by a person since: kept


class MainTest(unittest.TestCase):
    def setUp(self):
        self.saved = {k: getattr(pr_status, k) for k in
                      ("graphql", "find_board_fields", "load_issue", "board_item", "set_field", "try_gh")}
        self.set, self.other_prs = [], []
        refs = [{"number": 1, "state": "OPEN", "repository": {"nameWithOwner": REPO}},
                {"number": 2, "state": "CLOSED", "repository": {"nameWithOwner": REPO}},
                {"number": 3, "state": "OPEN", "repository": {"nameWithOwner": "Akenon-Studio/handbook"}},
                None]
        def graphql(q, **v):
            if "closedByPullRequestsReferences" in q:
                return {"repository": {"issue": {"closedByPullRequestsReferences": {"nodes": self.other_prs}}}}
            return {"repository": {"pullRequest": {"closingIssuesReferences": {"nodes": refs}}}}
        pr_status.graphql = graphql
        pr_status.find_board_fields = lambda title: {"id": "B"}
        self.status = "In progress"
        pr_status.load_issue = lambda repo, n: {"n": f"{repo}#{n}", "state": "OPEN",
                                                "assignedActors": {"nodes": [{"login": "x"}]}}
        pr_status.board_item = lambda board, issue: (issue["n"], self.status)
        pr_status.set_field = lambda board, item, field, value: self.set.append((item, field, value))
        # the author can write to platform, not handbook
        pr_status.try_gh = lambda path: {"permission": "write" if "/platform/" in path else "read"}

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(pr_status, k, v)

    def run_event(self, e):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(e, f)
        return pr_status.main(f.name)

    def test_only_open_issues_where_the_author_can_write(self):
        self.assertEqual(self.run_event(event("opened")), 0)
        self.assertEqual(self.set, [(f"{REPO}#1", "Status", "In review")])

    def test_a_bot_only_in_its_own_repo(self):
        pr_status.try_gh = lambda path: self.fail("a bot has no collaborator permission to ask about")
        self.run_event(event("opened", login="renovate", user_type="Bot"))
        self.assertEqual(self.set, [(f"{REPO}#1", "Status", "In review")])

    def test_another_ready_pr_keeps_it_in_review(self):
        self.status = "In review"
        self.other_prs = [{"number": 9, "isDraft": False, "repository": {"nameWithOwner": REPO}}]
        self.run_event(event("closed"))
        self.assertEqual(self.set, [])
        self.other_prs = [{"number": 9, "isDraft": True, "repository": {"nameWithOwner": REPO}},
                          {"number": 7, "isDraft": False, "repository": {"nameWithOwner": REPO}}]  # this one
        self.run_event(event("closed"))
        self.assertEqual(self.set, [(f"{REPO}#1", "Status", "In progress")])

    def test_an_edit_that_stops_closing_an_issue_lets_it_go(self):
        self.status = "In review"
        e = event("edited")
        e["pull_request"]["body"] = "Closes #1"
        e["changes"] = {"body": {"from": "Closes #12\nCloses #1"}}
        self.run_event(e)
        self.assertIn((f"{REPO.lower()}#12", "Status", "In progress"), self.set)
        self.assertNotIn((f"{REPO}#1", "Status", "In progress"), self.set)  # still closed: stays

    def test_nothing_read_when_nothing_changes(self):
        pr_status.graphql = lambda q, **v: self.fail("no call for a merge")
        self.assertEqual(self.run_event(event("closed", merged=True)), 0)


if __name__ == "__main__":
    unittest.main()

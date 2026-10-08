"""Tests for scripts/issue_sweep.py: which issues the scheduled sweep re-syncs.
Run: python3 -m unittest discover tests"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from issue_fields import link_problems, load_forms, problems_text  # noqa: E402
from issue_sweep import in_managed_repo, out_of_date  # noqa: E402
from test_issue_fields import TASK, body  # noqa: E402

FORMS = load_forms()
BOARD = "PVT_board"


def issue(parent=True, status="Todo", blocked_by=0, labels=(), comments=(), repo="Akenon-Studio/handbook",
          author="User"):
    return {"number": 1, "title": "Write the guide", "body": body(**TASK), "issueType": {"name": "Task"},
            "author": {"__typename": author, "login": "someone"},
            "repository": {"nameWithOwner": repo},
            "labels": {"nodes": [{"name": l} for l in labels]},
            "parent": {"number": 24} if parent else None,
            "subIssues": {"nodes": []}, "blockedBy": {"totalCount": blocked_by},
            "projectItems": {"nodes": [{"id": "item", "project": {"id": BOARD},
                                        "fieldValueByName": {"name": status}}]},
            "comments": {"nodes": [{"body": c} for c in comments]}}


class OutOfDateTest(unittest.TestCase):
    def test_complete_unlabelled_issue_is_left_alone(self):
        self.assertFalse(out_of_date(issue(), FORMS, BOARD))

    def test_newly_blocked_without_a_link_is_synced(self):
        self.assertTrue(out_of_date(issue(status="Blocked"), FORMS, BOARD))

    def test_parent_removed_is_synced(self):
        self.assertTrue(out_of_date(issue(parent=False), FORMS, BOARD))

    def test_fixed_but_still_labelled_is_synced(self):
        self.assertTrue(out_of_date(issue(labels=["needs-fields"]), FORMS, BOARD))

    def test_bot_opened_issue_without_a_parent_is_left_alone(self):
        self.assertFalse(out_of_date(issue(parent=False, author="Bot"), FORMS, BOARD))

    def test_labelled_with_the_right_comment_is_left_alone(self):
        text = problems_text(link_problems("Task", False, 0, "Todo", 0))
        self.assertFalse(out_of_date(issue(parent=False, labels=["needs-fields"], comments=[text]),
                                     FORMS, BOARD))

    def test_labelled_with_a_stale_comment_is_synced(self):
        stale = problems_text(["**Why** is required but empty."])
        self.assertTrue(out_of_date(issue(parent=False, labels=["needs-fields"], comments=[stale]),
                                    FORMS, BOARD))


class ManagedRepoTest(unittest.TestCase):
    def test_only_managed_repos_in_the_org(self):
        managed = {"handbook"}
        self.assertTrue(in_managed_repo(issue(), managed))
        self.assertFalse(in_managed_repo(issue(repo="Akenon-Studio/old-thing"), managed))
        self.assertFalse(in_managed_repo(issue(repo="someone/handbook"), managed))


if __name__ == "__main__":
    unittest.main()

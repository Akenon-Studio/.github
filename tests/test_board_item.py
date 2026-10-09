"""Tests for issue_fields.board_item when the issue was added to the board a moment earlier by
another writer (peras#30). Run: python3 -m unittest discover tests"""

import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

import issue_fields  # noqa: E402

BOARD = {"id": "P1"}
ISSUE = {"id": "I1", "number": 5, "repository": {"nameWithOwner": "Akenon-Studio/handbook"},
         "projectItems": {"nodes": []}}
ON_BOARD = {**ISSUE, "projectItems": {"nodes": [
    {"id": "ITEM1", "project": {"id": "P1"}, "fieldValueByName": {"name": "Todo"}}]}}


class BoardItemRaceTest(unittest.TestCase):
    def test_uses_the_item_another_writer_added(self):
        refuse = mock.Mock(side_effect=SystemExit("gh: Content already exists in this project"))
        with mock.patch.object(issue_fields, "graphql", refuse), \
             mock.patch.object(issue_fields, "load_issue", return_value=ON_BOARD), \
             mock.patch.object(issue_fields.time, "sleep"):
            self.assertEqual(issue_fields.board_item(BOARD, ISSUE), ("ITEM1", "Todo"))

    def test_other_errors_still_fail(self):
        refuse = mock.Mock(side_effect=SystemExit("gh: something else"))
        with mock.patch.object(issue_fields, "graphql", refuse):
            with self.assertRaises(SystemExit):
                issue_fields.board_item(BOARD, ISSUE)

    def test_adds_when_not_on_the_board(self):
        added = mock.Mock(return_value={"addProjectV2ItemById": {"item": {"id": "NEW"}}})
        with mock.patch.object(issue_fields, "graphql", added):
            self.assertEqual(issue_fields.board_item(BOARD, ISSUE), ("NEW", None))


if __name__ == "__main__":
    unittest.main()

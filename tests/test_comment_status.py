"""Tests for scripts/comment_status.py. Run: python3 -m unittest discover tests"""

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

import comment_status  # noqa: E402
from comment_status import decide, waiting_on  # noqa: E402


class WaitingLineTest(unittest.TestCase):
    def test_a_waiting_on_line(self):
        self.assertEqual(waiting_on("Done: x.\nWaiting on: @Thytus777 to pick the supplier"),
                         "@Thytus777 to pick the supplier")
        self.assertEqual(waiting_on("Waiting on the owner: (1) approve #106"), "the owner: (1) approve #106")
        self.assertEqual(waiting_on("- **Waiting on:** a decision"), "a decision")

    def test_nothing_and_quotes_dont_count(self):
        for body in ("Waiting on: nothing", "Waiting on: none.", "Waiting on: -", "Waiting on:",
                     "> Waiting on: you\nThanks, done.", "`Waiting on: x`", "```\nWaiting on: x\n```",
                     "~~~\nWaiting on: x\n~~~", "```\nWaiting on: x", "    Waiting on: x (indented code)",
                     "Not waiting on anyone", ""):
            self.assertIsNone(waiting_on(body), body)


class DecideTest(unittest.TestCase):
    def test_waiting_sets_the_status_and_assigns_who_it_names(self):
        self.assertEqual(decide("Waiting on: @a and @b-c, not me@example.com", "In progress", True),
                         ("Waiting for human", ["a", "b-c"]))
        self.assertEqual(decide("Waiting on: review", "Waiting for human", True), (None, []))
        for kept in ("Done", "In review", "Blocked"):  # a PR in review, a blocked issue: kept
            self.assertEqual(decide("Waiting on: @owner to review #110", kept, True), (None, ["owner"]))

    def test_any_other_comment_takes_it_back(self):
        self.assertEqual(decide("Chose B.", "Waiting for human", True), ("In progress", []))
        self.assertEqual(decide("Chose B.", "Waiting for human", False), ("Todo", []))
        self.assertEqual(decide("Chose B.", "In review", True), (None, []))


class MainTest(unittest.TestCase):
    def setUp(self):
        self.saved = {k: getattr(comment_status, k) for k in
                      ("find_board_fields", "load_issue", "board_item", "set_field", "gh")}
        self.calls = []
        comment_status.find_board_fields = lambda title: {"id": "B"}
        comment_status.load_issue = lambda repo, n: {"assignedActors": {"nodes": [{"login": "x"}]}}
        comment_status.board_item = lambda board, issue: ("I", "In progress")
        comment_status.set_field = lambda board, item, field, value: self.calls.append((field, value))
        comment_status.gh = lambda path, *a, body=None: self.calls.append((path, body))

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(comment_status, k, v)

    def run_event(self, body, action="created", user_type="User", association="MEMBER", **issue):
        e = {"action": action, "repository": {"full_name": "Akenon-Studio/platform"},
             "issue": {"number": 5, "state": "open", **issue},
             "comment": {"body": body, "user": {"type": user_type}, "author_association": association}}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(e, f)
        return comment_status.main(f.name)

    def test_a_waiting_comment(self):
        self.assertEqual(self.run_event("Waiting on: @partner"), 0)
        self.assertEqual(self.calls, [("repos/Akenon-Studio/platform/issues/5/assignees", {"assignees": ["partner"]}),
                                      ("Status", "Waiting for human")])

    def test_pr_comments_change_nothing(self):
        comment_status.find_board_fields = lambda title: self.fail("no board call for a PR comment")
        self.run_event("Waiting on: @partner", pull_request={})
        self.run_event("Waiting on: @partner", state="closed")
        self.run_event("Waiting on: @partner", user_type="Bot")
        self.run_event("Waiting on: @partner", action="edited")
        for outsider in ("CONTRIBUTOR", "NONE", "FIRST_TIME_CONTRIBUTOR"):
            self.run_event("Waiting on: @me", association=outsider)


if __name__ == "__main__":
    unittest.main()

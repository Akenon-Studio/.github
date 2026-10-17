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

    def test_a_cycle_waiting_on_a_person_keeps_waiting_for_human(self):
        for peras in ("waiting: plan approval (link)", "Waiting on the owner", "  waiting"):
            self.assertEqual(decide("Chose B.", "Waiting for human", True, peras), (None, []), peras)
        for peras in (None, "", "usage limit until 14:00", "running stage 3", "not waiting"):
            self.assertEqual(decide("Chose B.", "Waiting for human", True, peras), ("In progress", []), peras)
        self.assertEqual(decide("Waiting on: @a", "In progress", True, "waiting: x"), ("Waiting for human", ["a"]))


class MainTest(unittest.TestCase):
    def setUp(self):
        self.saved = {k: getattr(comment_status, k) for k in
                      ("find_board_fields", "load_issue", "board_item", "set_field", "sync_reason", "gh", "try_gh")}
        self.calls = []
        comment_status.find_board_fields = lambda title: {"id": "B"}
        comment_status.load_issue = lambda repo, n: {"assignedActors": {"nodes": [{"login": "x"}]},
                                                     "projectItems": {"nodes": []}}
        comment_status.board_item = lambda board, issue: ("I", "In progress")
        comment_status.set_field = lambda board, item, field, value: self.calls.append((field, value))
        comment_status.sync_reason = lambda board, item, issue, status: self.calls.append(("Reason for", status))
        comment_status.gh = lambda path, *a, body=None: self.calls.append((path, body))
        self.writers = set()
        comment_status.try_gh = lambda path: {"permission": "write" if path.split("/")[-2] in self.writers else "read"}

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(comment_status, k, v)

    def run_event(self, body, action="created", user_type="User", association="MEMBER", login="someone", **issue):
        e = {"action": action, "repository": {"full_name": "Akenon-Studio/platform"},
             "issue": {"number": 5, "state": "open", **issue},
             "comment": {"body": body, "user": {"type": user_type, "login": login},
                         "author_association": association}}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(e, f)
        return comment_status.main(f.name)

    def test_a_waiting_comment(self):
        self.assertEqual(self.run_event("Waiting on: @partner"), 0)
        self.assertEqual(self.calls, [("repos/Akenon-Studio/platform/issues/5/assignees", {"assignees": ["partner"]}),
                                      ("Status", "Waiting for human"),
                                      ("Reason for", "Waiting for human")])

    def test_a_private_member_with_write_access_counts(self):
        self.writers = {"someone"}
        self.run_event("Waiting on: @partner", association="CONTRIBUTOR")
        self.assertIn(("Status", "Waiting for human"), self.calls)

    def test_pr_comments_change_nothing(self):
        comment_status.find_board_fields = lambda title: self.fail("no board call for a PR comment")
        self.run_event("Waiting on: @partner", pull_request={})
        self.run_event("Waiting on: @partner", state="closed")
        self.run_event("Waiting on: @partner", user_type="Bot")
        self.run_event("Waiting on: @partner", user_type="Bot", login="renovate[bot]")
        self.run_event("Waiting on: @partner", user_type="User", login="akenon-studio-peras", association="NONE")
        self.run_event("Waiting on: @partner", action="edited")
        for outsider in ("CONTRIBUTOR", "NONE", "FIRST_TIME_CONTRIBUTOR"):
            self.run_event("Waiting on: @me", association=outsider)

    def test_the_peras_apps_comments_count(self):
        comment_status.try_gh = lambda path: self.fail("a Peras app needs no permission check")
        for app in ("akenon-studio-peras[bot]", "Akenon-Studio-Peras-Reader[bot]"):
            self.calls = []
            self.run_event("Paused.\nWaiting on: @owner to approve the plan", user_type="Bot", login=app,
                           association="NONE")
            self.assertEqual(self.calls, [("repos/Akenon-Studio/platform/issues/5/assignees", {"assignees": ["owner"]}),
                                          ("Status", "Waiting for human"),
                                          ("Reason for", "Waiting for human")], app)

    def test_the_peras_field_keeps_waiting_for_human(self):
        comment_status.board_item = lambda board, issue: ("I", "Waiting for human")
        item = {"project": {"id": "B"}, "peras": {"text": "waiting: plan approval"}}
        comment_status.load_issue = lambda repo, n: {"assignedActors": {"nodes": [{"login": "x"}]},
                                                     "projectItems": {"nodes": [item]}}
        self.run_event("Approved.")
        self.assertEqual(self.calls, [])
        item["peras"] = {"text": ""}  # Peras moved on and cleared it: the next comment takes it back
        self.run_event("Moved on to stage 4.", user_type="Bot", login="akenon-studio-peras[bot]", association="NONE")
        self.assertEqual(self.calls, [("Status", "In progress"), ("Reason for", "In progress")])


if __name__ == "__main__":
    unittest.main()

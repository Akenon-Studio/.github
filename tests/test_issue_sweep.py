"""Tests for scripts/issue_sweep.py: which issues the scheduled sweep re-syncs, and the comment it
keeps on a PR that conflicts with its base branch.
Run: python3 -m unittest discover tests"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from issue_fields import all_problems, link_problems, load_forms, problems_text  # noqa: E402
from issue_sweep import (PR_MARKER, both_sides, conflict_text, in_managed_repo,  # noqa: E402
                         out_of_date, pr_action, resolved_text, who_to_tell)
from rules import automation_owners  # noqa: E402
from test_issue_fields import AUDIT, TASK, body, labelled  # noqa: E402

FORMS = load_forms()
BOARD = "PVT_board"


def issue(parent=True, status="Todo", blocked_by=0, labels=(), comments=(), repo="Akenon-Studio/handbook",
          author="User", assignees=("someone",), ever=("someone",), issue_type="Task", answers=None,
          label_events=(), subs=(0, 0)):
    return {"number": 1, "state": "OPEN", "body": body(**(answers or TASK)), "issueType": {"name": issue_type},
            "author": {"__typename": author, "login": "someone"},
            "assignedActors": {"nodes": [{"__typename": "User", "login": a} for a in assignees]},
            "timelineItems": {"nodes": [{"assignee": {"__typename": "User", "login": a}}
                                        for a in ever]},
            "repository": {"nameWithOwner": repo},
            "labels": {"nodes": [{"name": l} for l in labels]},
            "labelEvents": {"nodes": list(label_events)},
            "parent": {"number": 24} if parent else None,
            "subIssues": {"nodes": []}, "blockedBy": {"totalCount": blocked_by},
            "subIssuesSummary": {"total": subs[0], "completed": subs[1]},
            "projectItems": {"nodes": [{"id": "item", "project": {"id": BOARD},
                                        "fieldValueByName": {"name": status}}]},
            "comments": {"nodes": [{"body": c} for c in comments]}}


def with_epic(item, on_board):
    item["parent"] = {"number": 136, "title": "Phase 2", "issueType": {"name": "Epic"},
                      "repository": {"nameWithOwner": "Akenon-Studio/handbook"}, "parent": None}
    item["projectItems"]["nodes"][0]["epic"] = {"name": on_board} if on_board else None
    return item


class OutOfDateTest(unittest.TestCase):
    def test_epic_value_behind_the_parents_is_synced(self):
        self.assertTrue(out_of_date(with_epic(issue(), None), FORMS, BOARD, epic_field=True))
        self.assertTrue(out_of_date(with_epic(issue(), "Old"), FORMS, BOARD, epic_field=True))

    def test_epic_value_matching_the_parents_is_left_alone(self):
        self.assertFalse(out_of_date(with_epic(issue(), "Phase 2"), FORMS, BOARD, epic_field=True))

    def test_epic_ignored_until_the_board_has_the_field(self):
        self.assertFalse(out_of_date(with_epic(issue(), None), FORMS, BOARD))

    def test_complete_unlabelled_issue_is_left_alone(self):
        self.assertFalse(out_of_date(issue(), FORMS, BOARD))

    def test_newly_blocked_without_a_link_is_synced(self):
        self.assertTrue(out_of_date(issue(status="Blocked"), FORMS, BOARD))

    def test_parent_removed_is_synced(self):
        self.assertTrue(out_of_date(issue(parent=False), FORMS, BOARD))

    def test_finding_confirmed_since_the_last_run_is_synced(self):
        flagged = issue(issue_type="Audit finding", answers=AUDIT, status="Waiting for human")
        text = problems_text(all_problems(flagged, FORMS, "Waiting for human"))
        self.assertFalse(out_of_date(issue(issue_type="Audit finding", answers=AUDIT,
                                           status="Waiting for human", labels=["needs-fields"],
                                           comments=[text]), FORMS, BOARD))
        self.assertTrue(out_of_date(issue(issue_type="Audit finding", answers=AUDIT,
                                          status="Waiting for human",
                                          labels=["needs-fields", "confirmed"], comments=[text],
                                          label_events=[labelled("confirmed", "User")]),
                                    FORMS, BOARD))

    def test_last_sub_issue_closed_since_the_last_run_is_synced(self):
        # Closing a sub-issue sends its parent no event, so the sweep is what flags the parent.
        self.assertFalse(out_of_date(issue(subs=(3, 2)), FORMS, BOARD))
        self.assertTrue(out_of_date(issue(subs=(3, 3)), FORMS, BOARD))

    def test_parent_the_automation_reopened_is_synced_to_close_it(self):
        done = dict(issue(subs=(2, 2), labels=["needs-fields"]),
                    reopenEvents={"nodes": [{"actor": {"__typename": "Bot",
                                                     "login": "akenon-studio-automation"}}]})
        self.assertTrue(out_of_date(done, FORMS, BOARD))

    def test_fixed_but_still_labelled_is_synced(self):
        self.assertTrue(out_of_date(issue(labels=["needs-fields"]), FORMS, BOARD))

    def test_bot_opened_issue_without_a_parent_is_left_alone(self):
        self.assertFalse(out_of_date(issue(parent=False, author="Bot", labels=["settings-drift"]),
                                     FORMS, BOARD))

    def test_bot_opened_issue_without_a_source_label_is_synced(self):
        self.assertTrue(out_of_date(issue(parent=False, author="Bot"), FORMS, BOARD))

    def test_author_never_assigned_is_synced(self):
        self.assertTrue(out_of_date(issue(assignees=(), ever=()), FORMS, BOARD))

    def test_author_assigned_then_unassigned_is_left_alone(self):
        self.assertFalse(out_of_date(issue(assignees=()), FORMS, BOARD))

    def test_waiting_for_human_unassigned_is_synced(self):
        self.assertTrue(out_of_date(issue(status="Waiting for human", assignees=()), FORMS, BOARD))

    def test_waiting_for_human_assigned_is_left_alone(self):
        self.assertFalse(out_of_date(issue(status="Waiting for human"), FORMS, BOARD))

    def test_labelled_with_the_right_comment_is_left_alone(self):
        text = problems_text(link_problems("Task", False, "Todo", 0))
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


def pr(mergeable="CONFLICTING", assignees=("alice",), author="User", comments=()):
    return {"number": 7, "mergeable": mergeable, "baseRefName": "main", "headRefOid": "h",
            "author": {"__typename": author, "login": "bob"},
            "repository": {"nameWithOwner": "Akenon-Studio/animations"},
            "assignees": {"nodes": [{"login": a} for a in assignees]},
            "comments": {"nodes": [{"databaseId": i, "body": c} for i, c in enumerate(comments, 1)]}}


class ConflictNoticeTest(unittest.TestCase):
    def test_new_conflict_posts_a_notice_naming_the_files(self):
        action = pr_action(pr(), ["scripts/a.py"])
        self.assertEqual(action[0], "post")
        self.assertTrue(action[1].startswith(f"{PR_MARKER}\n@alice This PR conflicts with `main`"))
        self.assertIn("- `scripts/a.py`", action[1])
        self.assertIn("git merge origin/main", action[1])
        self.assertIn("don't rebase and force-push", action[1])

    def test_without_the_base_files_it_names_only_the_base(self):
        text = pr_action(pr(), None)[1]
        self.assertIn("conflicts with `main`", text)
        self.assertNotIn("These files", text)

    def test_unchanged_notice_is_left_alone(self):
        text = conflict_text(pr(), ["scripts/a.py"])
        self.assertIsNone(pr_action(pr(comments=["hi", text]), ["scripts/a.py"]))

    def test_notice_is_edited_when_the_files_change(self):
        old = conflict_text(pr(), ["scripts/a.py"])
        action = pr_action(pr(comments=["hi", old]), ["README.md", "scripts/a.py"])
        self.assertEqual(action, ("edit", 2, conflict_text(pr(), ["README.md", "scripts/a.py"])))

    def test_resolved_conflict_edits_the_notice_and_keeps_it(self):
        old = conflict_text(pr(), ["scripts/a.py"])
        action = pr_action(pr(mergeable="MERGEABLE", comments=[old]))
        self.assertEqual(action, ("edit", 1, resolved_text(pr(), old)))
        self.assertIn("**Resolved:** this PR no longer conflicts with `main`", action[2])
        self.assertIn("- `scripts/a.py`", action[2])

    def test_resolved_notice_is_left_alone(self):
        done = resolved_text(pr(), conflict_text(pr(), []))
        self.assertIsNone(pr_action(pr(mergeable="MERGEABLE", comments=[done])))

    def test_conflicting_again_after_resolved_posts_a_new_notice(self):
        done = resolved_text(pr(), conflict_text(pr(), []))
        self.assertEqual(pr_action(pr(comments=[done]), [])[0], "post")

    def test_unknown_is_skipped_until_github_has_computed_it(self):
        self.assertIsNone(pr_action(pr(mergeable="UNKNOWN"), []))
        old = conflict_text(pr(), [])
        self.assertIsNone(pr_action(pr(mergeable="UNKNOWN", comments=[old]), []))

    def test_mergeable_pr_never_flagged_is_left_alone(self):
        self.assertIsNone(pr_action(pr(mergeable="MERGEABLE")))

    def test_someone_elses_comment_is_not_ours(self):
        self.assertEqual(pr_action(pr(comments=["conflicts with main, fix please"]), [])[0], "post")


class WhoToTellTest(unittest.TestCase):
    def test_assignees(self):
        self.assertEqual(who_to_tell(pr(assignees=("alice", "carol"))), ["alice", "carol"])

    def test_bot_pr_without_assignee_goes_to_the_automation_owners(self):
        self.assertEqual(who_to_tell(pr(assignees=(), author="Bot")), automation_owners())

    def test_persons_pr_without_assignee_goes_to_the_author(self):
        self.assertEqual(who_to_tell(pr(assignees=())), ["bob"])

    def test_mentions_appear_in_the_notice(self):
        owners = " ".join("@" + o for o in automation_owners())
        self.assertIn(owners, conflict_text(pr(assignees=(), author="Bot"), []))


class BothSidesTest(unittest.TestCase):
    def test_sorted_intersection(self):
        self.assertEqual(both_sides(["b", "a", "c"], ["c", "a", "z"]), ["a", "c"])


if __name__ == "__main__":
    unittest.main()

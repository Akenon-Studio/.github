"""Tests for scripts/bot_pr_review.py: which open PRs the automation owners are asked to review.
Run: python3 -m unittest discover tests"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from bot_pr_review import board_changes, missing_source_label, needs_review  # noqa: E402

MANAGED = {"platform", "handbook"}


def pr(author="Bot", asked=(), repo="Akenon-Studio/platform", labels=(), login="renovate"):
    return {"number": 1, "author": {"__typename": author, "login": login},
            "repository": {"nameWithOwner": repo}, "labels": {"nodes": [{"name": l} for l in labels]},
            "timelineItems": {"nodes": [{"requestedReviewer": {"__typename": "Team", "slug": s}}
                                        for s in asked]}}


class NeedsReviewTest(unittest.TestCase):
    def test_bot_pr_never_asked(self):
        self.assertTrue(needs_review(pr(), MANAGED))

    def test_bot_pr_already_asked(self):
        self.assertFalse(needs_review(pr(asked=["automation-owners"]), MANAGED))

    def test_another_team_asked_still_needs_ours(self):
        self.assertTrue(needs_review(pr(asked=["engineers"]), MANAGED))

    def test_a_persons_pr_is_left_alone(self):
        self.assertFalse(needs_review(pr(author="User"), MANAGED))

    def test_only_managed_repos_in_the_org(self):
        self.assertFalse(needs_review(pr(repo="Akenon-Studio/old-thing"), MANAGED))
        self.assertFalse(needs_review(pr(repo="someone/platform"), MANAGED))

    def test_a_person_requested_is_not_the_team(self):
        p = pr()
        p["timelineItems"]["nodes"].append({"requestedReviewer": {"__typename": "User"}})
        p["timelineItems"]["nodes"].append({"requestedReviewer": None})
        self.assertTrue(needs_review(p, MANAGED))


SOURCES = {"renovate": {"labels": ["dependencies"], "no_issue": True}}


class SourceLabelTest(unittest.TestCase):
    def test_unlabelled_renovate_pr_gets_its_label(self):
        self.assertEqual(missing_source_label(pr(), MANAGED, SOURCES), "dependencies")

    def test_labelled_pr_is_left_alone(self):
        self.assertIsNone(missing_source_label(pr(labels=["dependencies"]), MANAGED, SOURCES))

    def test_unlisted_bots_and_people_are_left_alone(self):
        self.assertIsNone(missing_source_label(pr(login="someapp"), MANAGED, SOURCES))
        self.assertIsNone(missing_source_label(pr(author="User"), MANAGED, SOURCES))


def item(status=None, why=None, board="B"):
    return {"id": "item", "project": {"id": board}, "status": {"name": status} if status else None,
            "reason": {"name": why} if why else None}


class BoardChangesTest(unittest.TestCase):
    def changes(self, items=(), labels=(), problems=()):
        p = pr(labels=labels)
        p["projectItems"] = {"nodes": list(items)}
        return board_changes(p, "B", list(problems))

    def test_a_new_pr_is_added_in_review_with_reason_review(self):
        self.assertEqual(self.changes(), (None, "In review", "Review"))

    def test_on_another_board_counts_as_not_on_ours(self):
        self.assertEqual(self.changes([item("Todo", board="other")]), (None, "In review", "Review"))

    def test_a_status_someone_set_is_kept(self):
        found, status, why = self.changes([item("Done", "Review")])
        self.assertEqual((found, status, why), (("item", "Done", "Review"), None, "Review"))

    def test_problems_make_it_needs_fields_and_fixing_them_clears_it(self):
        self.assertEqual(self.changes(problems=["x"])[2], "Needs fields")
        self.assertEqual(self.changes([item("In review", "Needs fields")], ["needs-fields"])[2], "Review")


if __name__ == "__main__":
    unittest.main()

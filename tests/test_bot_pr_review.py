"""Tests for scripts/bot_pr_review.py: which open PRs the automation owners are asked to review.
Run: python3 -m unittest discover tests"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

import bot_pr_review  # noqa: E402
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

    def test_a_status_a_person_set_is_kept_but_done_is_reset(self):
        found, status, why = self.changes([item("In review", "Review")])
        self.assertEqual((found, status, why), (("item", "In review", "Review"), None, "Review"))
        self.assertIsNone(self.changes([item("Blocked", "Review")])[1])
        self.assertEqual(self.changes([item("Done", "Review")])[1], "In review")  # reopened
        self.assertEqual(self.changes([item(None, None)])[1], "In review")

    def test_problems_make_it_needs_fields_and_fixing_them_clears_it(self):
        self.assertEqual(self.changes(problems=["x"])[2], "Needs fields")
        self.assertEqual(self.changes([item("In review", "Needs fields")], ["needs-fields"])[2], "Review")


class BoardPassTest(unittest.TestCase):
    def setUp(self):
        self.saved = {k: getattr(bot_pr_review, k) for k in
                      ("find_board_fields", "report", "set_field", "set_reason", "graphql")}
        self.calls = []
        bot_pr_review.find_board_fields = lambda title: {"id": "B"}
        bot_pr_review.report = lambda repo, n, p, problems, what: self.calls.append(("report", n, problems))
        bot_pr_review.set_field = lambda board, item, f, v: self.calls.append((f, v))
        bot_pr_review.set_reason = lambda board, item, cur, want: self.calls.append(("Reason", cur, want))
        bot_pr_review.graphql = lambda q, **v: {"addProjectV2ItemById": {"item": {"id": "new"}}}

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(bot_pr_review, k, v)

    def bot_pr(self, n=1, closes=0, items=(), labels=(), draft=False):
        p = pr(labels=labels)
        p.update(number=n, id=f"PR{n}", isDraft=draft, projectItems={"nodes": list(items)},
                 closingIssuesReferences={"totalCount": closes})
        return p

    def test_a_new_renovate_pr_goes_on_in_review(self):
        self.assertEqual(bot_pr_review.board_pass([self.bot_pr()], MANAGED, SOURCES, False), 0)
        self.assertEqual(self.calls, [("report", 1, []), ("Status", "In review"),
                                      ("Reason", None, "Review")])

    def test_one_already_right_is_left_alone(self):
        bot_pr_review.board_pass([self.bot_pr(items=[item("In review", "Review")])], MANAGED, SOURCES, False)
        self.assertEqual(self.calls, [])

    def test_one_that_stops_qualifying_is_taken_off(self):
        bot_pr_review.board_pass([self.bot_pr(draft=True, items=[item("In review", "Review")])],
                                 MANAGED, SOURCES, False)
        self.assertEqual(self.calls, [("Reason", "Review", None)])

    def test_a_flagged_pr_off_the_board_is_unflagged_when_it_stops_qualifying(self):
        bot_pr_review.board_pass([self.bot_pr(draft=True, labels=["needs-fields"])], MANAGED, SOURCES, False)
        self.assertEqual(self.calls, [("report", 1, [])])

    def test_one_failure_doesnt_stop_the_rest(self):
        def boom(*a):
            raise SystemExit("GraphQL error")
        bot_pr_review.report = lambda repo, n, p, problems, what: boom() if n == 1 else None
        failed = bot_pr_review.board_pass([self.bot_pr(1), self.bot_pr(2)], MANAGED, SOURCES, False)
        self.assertEqual(failed, 1)
        self.assertIn(("Reason", None, "Review"), self.calls)


if __name__ == "__main__":
    unittest.main()
